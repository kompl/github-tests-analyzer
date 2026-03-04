package collect

import (
	"archive/zip"
	"bytes"
	"fmt"
	"io"
	"log"
	"regexp"
	"strconv"
	"strings"

	"filedecorator/v2/internal"
)

// LogExtractor parses zip logs from GitHub Actions to extract failed test results.
// This is the equivalent of Python's LogTestResultsExtractor.
type LogExtractor struct {
	patternPublishGroup *regexp.Regexp
	patternTestResults  *regexp.Regexp
	patternTestLine     *regexp.Regexp
	patternErrorLine    *regexp.Regexp
	patternEndGroup     *regexp.Regexp
	patternNoTests      *regexp.Regexp
}

// NewLogExtractor creates a new LogExtractor with compiled regex patterns.
func NewLogExtractor() *LogExtractor {
	return &LogExtractor{
		patternPublishGroup: regexp.MustCompile(`##\[group\]🚀 Publish results`),
		patternTestResults: regexp.MustCompile(
			`ℹ️ - test results (.*?) - (\d+) tests run, (\d+) passed, (\d+) skipped, (\d+) failed`,
		),
		patternTestLine:  regexp.MustCompile(`.*?🧪 - (.*?)(?:\s*\|\s*(.*))?$`),
		patternErrorLine: regexp.MustCompile(`##\[error\](.*)$`),
		patternEndGroup:  regexp.MustCompile(`##\[endgroup\]`),
		patternNoTests:   regexp.MustCompile(`(?i)No test results found`),
	}
}

// ParseZip parses zip log bytes and returns (failed_details, hasNoTests).
func (le *LogExtractor) ParseZip(zipBytes []byte) (map[string][]internal.TestDetail, bool) {
	r, err := zip.NewReader(bytes.NewReader(zipBytes), int64(len(zipBytes)))
	if err != nil {
		log.Printf("[logs] Bad zip: %v", err)
		return nil, true
	}

	failed := make(map[string][]internal.TestDetail)
	hasNoTests := false
	orderPos := 0

	for _, f := range r.File {
		if !strings.HasSuffix(strings.ToLower(f.Name), ".txt") {
			continue
		}

		lines, err := readZipFileLines(f)
		if err != nil {
			continue
		}

		// Quick scan for "No test results"
		for _, ln := range lines {
			if le.patternNoTests.MatchString(ln) {
				hasNoTests = true
				break
			}
		}
		if hasNoTests {
			break
		}

		i := 0
		for i < len(lines) {
			line := lines[i]

			if !le.patternPublishGroup.MatchString(line) {
				i++
				continue
			}

			// Next line should be test statistics
			i++
			if i >= len(lines) {
				break
			}

			statMatch := le.patternTestResults.FindStringSubmatch(lines[i])
			if statMatch == nil {
				continue
			}

			projectName := statMatch[1]
			failedCount, _ := strconv.Atoi(statMatch[5])

			if failedCount == 0 {
				// Skip to end of group
				for i < len(lines) && !le.patternEndGroup.MatchString(lines[i]) {
					i++
				}
				continue
			}

			// Collect failed tests (lines with 🧪)
			type testEntry struct {
				name       string
				desc       string
				orderIndex int
			}
			var failedTests []testEntry

			i++
			for i < len(lines) {
				testMatch := le.patternTestLine.FindStringSubmatch(lines[i])
				if testMatch == nil {
					break
				}

				testKey := strings.TrimSpace(testMatch[1])
				description := ""
				if len(testMatch) > 2 {
					description = strings.TrimSpace(testMatch[2])
				}

				// Join key and description with " | " (same as Python)
				testName := testKey + " | " + description
				failedTests = append(failedTests, testEntry{
					name:       testName,
					desc:       description,
					orderIndex: orderPos,
				})
				orderPos++
				i++
			}

			// Collect error sections (##[error])
			var errors []string
			for i < len(lines) {
				if le.patternEndGroup.MatchString(lines[i]) {
					break
				}
				errorMatch := le.patternErrorLine.FindStringSubmatch(lines[i])
				if errorMatch == nil {
					i++
					continue
				}

				errorDesc := strings.TrimSpace(errorMatch[1])
				var detailLines []string
				i++
				for i < len(lines) {
					if le.patternErrorLine.MatchString(lines[i]) || le.patternEndGroup.MatchString(lines[i]) {
						break
					}
					detailLines = append(detailLines, lines[i])
					i++
				}
				detailsText := strings.TrimSpace(strings.Join(detailLines, "\n"))
				errors = append(errors, fmt.Sprintf("\n%s\n%s\n---\n", errorDesc, detailsText))
			}

			// Map errors to tests
			for idx, te := range failedTests {
				if idx >= len(errors) {
					break
				}
				context := strings.TrimSpace(errors[idx])
				if context == "" {
					continue
				}
				failed[te.name] = append(failed[te.name], internal.TestDetail{
					File:       f.Name,
					LineNum:    0,
					Context:    context,
					Project:    projectName,
					OrderIndex: te.orderIndex,
				})
			}
		}
	}

	hasNoTests = len(failed) == 0
	return failed, hasNoTests
}

func readZipFileLines(f *zip.File) ([]string, error) {
	rc, err := f.Open()
	if err != nil {
		return nil, err
	}
	defer rc.Close()

	data, err := io.ReadAll(rc)
	if err != nil {
		return nil, err
	}

	// Handle both \r\n and \n line endings; rstrip each line (same as Python's .rstrip())
	text := strings.ReplaceAll(string(data), "\r\n", "\n")
	text = strings.ReplaceAll(text, "\r", "\n")
	lines := strings.Split(text, "\n")
	for i, line := range lines {
		lines[i] = strings.TrimRight(line, " \t\r")
	}
	return lines, nil
}
