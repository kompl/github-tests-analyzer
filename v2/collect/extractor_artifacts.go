package collect

import (
	"archive/zip"
	"bytes"
	"encoding/xml"
	"fmt"
	"io"
	"log"
	"strings"

	"filedecorator/v2/internal"
)

// ArtifactExtractor parses JUnit XML from GitHub Actions artifacts.
// Equivalent of Python's ArtifactsTestResultsExtractor.
type ArtifactExtractor struct {
	gh *GitHubClient
}

// NewArtifactExtractor creates a new ArtifactExtractor.
func NewArtifactExtractor(gh *GitHubClient) *ArtifactExtractor {
	return &ArtifactExtractor{gh: gh}
}

// Extract downloads and parses test-reports-* artifacts for a run.
func (ae *ArtifactExtractor) Extract(repo string, runID int) (map[string][]internal.TestDetail, bool) {
	artifacts, err := ae.gh.ListRunArtifacts(repo, runID)
	if err != nil {
		log.Printf("[artifacts] Error listing artifacts for run %d: %v", runID, err)
		return nil, true
	}

	if len(artifacts) == 0 {
		fmt.Printf("  [artifacts] No artifacts for run %d\n", runID)
		return nil, true
	}

	// Filter test-reports-* artifacts
	var reportArtifacts []ghArtifact
	for _, a := range artifacts {
		if strings.HasPrefix(a.Name, "test-reports-") && !a.Expired {
			reportArtifacts = append(reportArtifacts, a)
		}
	}

	if len(reportArtifacts) == 0 {
		fmt.Printf("  [artifacts] No test-reports-* artifacts for run %d\n", runID)
		return nil, true
	}

	combined := make(map[string][]internal.TestDetail)
	foundAnyJUnit := false
	globalPos := 0
	firstSeenOrder := make(map[string]int)

	for _, art := range reportArtifacts {
		project := strings.TrimPrefix(art.Name, "test-reports-")
		if art.ArchiveDownloadURL == "" {
			continue
		}

		zipBytes, err := ae.gh.DownloadArtifact(art.ArchiveDownloadURL)
		if err != nil {
			log.Printf("[artifacts] Error downloading artifact '%s' for run %d: %v", art.Name, runID, err)
			continue
		}

		parsed, hasJUnit := ae.parseJUnitZip(zipBytes, project)
		if hasJUnit {
			foundAnyJUnit = true
		}

		for k, v := range parsed {
			if _, seen := firstSeenOrder[k]; !seen {
				firstSeenOrder[k] = globalPos
				globalPos++
			}
			combined[k] = append(combined[k], v...)
		}
	}

	hasNoTests := !foundAnyJUnit
	if hasNoTests {
		fmt.Printf("  [artifacts] No JUnit reports found in artifacts for run %d\n", runID)
	}
	return combined, hasNoTests
}

// junitTestSuites represents a JUnit XML file root (can be <testsuites> or <testsuite>).
type junitTestSuites struct {
	XMLName    xml.Name         `xml:""`
	TestSuites []junitTestSuite `xml:"testsuite"`
	TestCases  []junitTestCase  `xml:"testcase"`
}

type junitTestSuite struct {
	TestCases  []junitTestCase  `xml:"testcase"`
	TestSuites []junitTestSuite `xml:"testsuite"`
}

type junitTestCase struct {
	ClassName string         `xml:"classname,attr"`
	Name      string         `xml:"name,attr"`
	Failures  []junitFailure `xml:"failure"`
	Errors    []junitFailure `xml:"error"`
}

type junitFailure struct {
	Message string `xml:"message,attr"`
	Text    string `xml:",chardata"`
}

func (ae *ArtifactExtractor) parseJUnitZip(zipBytes []byte, project string) (map[string][]internal.TestDetail, bool) {
	r, err := zip.NewReader(bytes.NewReader(zipBytes), int64(len(zipBytes)))
	if err != nil {
		log.Println("[artifacts] Bad zip for junit artifact")
		return nil, false
	}

	failed := make(map[string][]internal.TestDetail)
	foundAnyJUnit := false
	seenOrder := make(map[string]int)
	localPos := 0

	for _, f := range r.File {
		if !strings.HasSuffix(strings.ToLower(f.Name), ".xml") {
			continue
		}

		rc, err := f.Open()
		if err != nil {
			continue
		}
		data, err := io.ReadAll(rc)
		rc.Close()
		if err != nil {
			continue
		}

		parsed := parseJUnitXML(data)
		if parsed.hasAnyTestCase {
			foundAnyJUnit = true
		}
		if len(parsed.failed) == 0 {
			continue
		}

		for _, tc := range parsed.failed {
			classname := strings.TrimSpace(tc.ClassName)
			name := strings.TrimSpace(tc.Name)

			var testKey string
			if classname != "" && name != "" {
				testKey = classname + "::" + name
			} else if name != "" {
				testKey = name
			} else if classname != "" {
				testKey = classname
			} else {
				testKey = "unknown"
			}

			if _, seen := seenOrder[testKey]; !seen {
				seenOrder[testKey] = localPos
				localPos++
			}

			allFailures := append(tc.Failures, tc.Errors...)
			for _, failure := range allFailures {
				message := strings.TrimSpace(failure.Message)
				detailsText := strings.TrimSpace(failure.Text)
				context := strings.Trim(fmt.Sprintf("\n%s\n%s\n---\n", message, detailsText), "\n")

				testName := testKey + " | " + message

				failed[testName] = append(failed[testName], internal.TestDetail{
					File:       f.Name,
					LineNum:    0,
					Context:    context,
					Project:    project,
					OrderIndex: seenOrder[testKey],
				})
			}
		}
	}

	return failed, foundAnyJUnit
}

// junitParseResult holds both the "has any testcase" flag and the failed subset.
type junitParseResult struct {
	hasAnyTestCase bool
	failed         []junitTestCase
}

// parseJUnitXML extracts all testcases from a JUnit XML, handling both
// <testsuites> and <testsuite> root elements.
// Returns whether any testcase was found (even passing) and the failed ones.
// In Python, found_any_junit is set to True when ANY <testcase> exists,
// even if all tests pass. This is important: a run with all passing tests
// is valid (has_no_tests=false), not "no tests".
func parseJUnitXML(data []byte) junitParseResult {
	// Try parsing as <testsuites>
	var suites junitTestSuites
	if err := xml.Unmarshal(data, &suites); err != nil {
		return junitParseResult{}
	}

	var allCases []junitTestCase

	// Collect direct testcases
	allCases = append(allCases, suites.TestCases...)

	// Collect from nested testsuites
	var collectFromSuites func([]junitTestSuite)
	collectFromSuites = func(suites []junitTestSuite) {
		for _, s := range suites {
			allCases = append(allCases, s.TestCases...)
			collectFromSuites(s.TestSuites)
		}
	}
	collectFromSuites(suites.TestSuites)

	// Filter to only failed/errored testcases
	var failed []junitTestCase
	for _, tc := range allCases {
		if len(tc.Failures) > 0 || len(tc.Errors) > 0 {
			failed = append(failed, tc)
		}
	}
	return junitParseResult{
		hasAnyTestCase: len(allCases) > 0,
		failed:         failed,
	}
}
