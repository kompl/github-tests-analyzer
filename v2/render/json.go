package render

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
	"unicode/utf8"

	"filedecorator/v2/analyze"
	"filedecorator/v2/config"
	"filedecorator/v2/internal"
)

// RenderJSON generates the combined JSON report for all repo/branch pairs.
func RenderJSON(results []RepoResult, cfg *config.Config) error {
	now := time.Now()
	report := jsonReport{
		GeneratedAt: now.Format(time.RFC3339),
		Projects:    make(map[string]jsonProject),
	}

	for _, r := range results {
		key := fmt.Sprintf("%s/%s", r.Repo, r.Branch)
		report.Projects[key] = buildRepoJSONData(r, cfg)
	}

	filename := fmt.Sprintf("report_%s.json", now.Format("20060102_150405"))
	reportPath := filepath.Join(cfg.Output.Dir, filename)
	if err := os.MkdirAll(filepath.Dir(reportPath), 0755); err != nil {
		return fmt.Errorf("mkdir: %w", err)
	}

	data, err := json.MarshalIndent(report, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal JSON: %w", err)
	}

	if err := os.WriteFile(reportPath, data, 0644); err != nil {
		return fmt.Errorf("write %s: %w", reportPath, err)
	}

	fmt.Printf("  [render] Generated JSON: %s\n", reportPath)
	return nil
}

type jsonReport struct {
	GeneratedAt string                 `json:"generated_at"`
	Projects    map[string]jsonProject `json:"projects"`
}

type jsonProject struct {
	Repo         string           `json:"repo"`
	Branch       string           `json:"branch"`
	MasterBranch string           `json:"master_branch"`
	LatestRun    jsonLatestRun    `json:"latest_run"`
	Summary      jsonSummary      `json:"summary"`
	FailedTests  []jsonFailedTest `json:"failed_tests"`
}

type jsonLatestRun struct {
	RunID       int    `json:"run_id"`
	SHA         string `json:"sha"`
	CommitTitle string `json:"commit_title"`
	Timestamp   string `json:"timestamp"`
	Conclusion  string `json:"conclusion"`
	Link        string `json:"link"`
	TotalFailed int    `json:"total_failed"`
}

type jsonSummary struct {
	TotalRunsAnalyzed  int `json:"total_runs_analyzed"`
	UniqueFailedTests  int `json:"unique_failed_tests"`
	MasterFailedTests  int `json:"master_failed_tests"`
	NewFailures        int `json:"new_failures"`
	StableFailingCount int `json:"stable_failing_count"`
	FixedCount         int `json:"fixed_count"`
	FlakyCount         int `json:"flaky_count"`
}

type jsonFailedTest struct {
	TestName       string              `json:"test_name"`
	ErrorMessage   string              `json:"error_message"`
	Classification string              `json:"classification"`
	InMaster       bool                `json:"in_master"`
	Project        string              `json:"project"`
	FailRatePct    *float64            `json:"fail_rate_pct,omitempty"`
	Pattern        string              `json:"pattern,omitempty"`
	ProbableCause  *jsonProbableCause  `json:"probable_cause,omitempty"`
	FailingSince   *jsonFailingSince   `json:"failing_since,omitempty"`
	FirstSeen      *jsonFirstSeen      `json:"first_seen_in_analysis,omitempty"`
	FlakyInfo      *jsonFlakyInfo      `json:"flaky_info,omitempty"`
}

type jsonProbableCause struct {
	SHA          string `json:"sha"`
	CommitTitle  string `json:"commit_title"`
	Timestamp    string `json:"timestamp"`
	RunLink      string `json:"run_link"`
	StreakLength int    `json:"streak_length"`
}

type jsonFailingSince struct {
	RunID int    `json:"run_id"`
	Date  string `json:"date"`
}

type jsonFirstSeen struct {
	Timestamp string `json:"timestamp"`
	Commit    string `json:"commit"`
	RunLink   string `json:"run_link"`
}

type jsonFlakyInfo struct {
	FailCount int `json:"fail_count"`
	TotalRuns int `json:"total_runs"`
}

func buildRepoJSONData(r RepoResult, cfg *config.Config) jsonProject {
	cr := r.Collect
	ar := r.Analyze
	er := r.Enrich

	stableFailing := ar.Behavior.StableFailing
	fixedTests := ar.Behavior.FixedTests
	flakyTests := ar.Behavior.FlakyTests

	allBehavior := make(map[string]*analyze.TestBehavior)
	for k, v := range stableFailing {
		allBehavior[k] = v
	}
	for k, v := range fixedTests {
		allBehavior[k] = v
	}
	for k, v := range flakyTests {
		allBehavior[k] = v
	}

	// Latest run info
	orderedKeys := cr.OrderedKeys
	var latestKey string
	if len(orderedKeys) > 0 {
		latestKey = orderedKeys[len(orderedKeys)-1]
	}
	latestMeta := cr.Meta[latestKey]
	latestFailed := cr.Summary[latestKey]

	// Build per-test entries
	testNames := latestFailed.ToSlice()
	sort.Strings(testNames)

	var failedTests []jsonFailedTest
	for _, testName := range testNames {
		detailsItems := cr.AllTestDetails[testName]
		errorMsg := extractError(detailsItems)
		project := extractProject(detailsItems)
		classification := classifyTest(testName, stableFailing, fixedTests, flakyTests)

		entry := jsonFailedTest{
			TestName:       testName,
			ErrorMessage:   errorMsg,
			Classification: classification,
			InMaster:       cr.MasterFailed.Contains(testName),
			Project:        project,
		}

		behavior := allBehavior[testName]
		if behavior != nil {
			total := max(behavior.TotalRuns, 1)
			rate := float64(behavior.FailCount) / float64(total) * 100
			roundedRate := float64(int(rate*10)) / 10
			entry.FailRatePct = &roundedRate
			entry.Pattern = behavior.Pattern
		}

		// Probable cause
		if behavior != nil {
			streak := findStreakStart(behavior.Pattern, orderedKeys, cr.Meta)
			if streak != nil {
				entry.ProbableCause = streak
			}
		}
		if entry.ProbableCause == nil && behavior == nil {
			entry.ProbableCause = &jsonProbableCause{
				SHA:          latestMeta.SHA,
				CommitTitle:  latestMeta.Title,
				Timestamp:    latestMeta.Timestamp,
				RunLink:      latestMeta.Link,
				StreakLength: 1,
			}
		}

		// Stable failing: add duration info
		if _, ok := stableFailing[testName]; ok {
			if er != nil {
				if since, ok := er.StableSince[testName]; ok {
					entry.FailingSince = &jsonFailingSince{
						RunID: since.RunID,
						Date:  since.CreatedAt,
					}
				}
			}
			sf := stableFailing[testName]
			if len(sf.FailedRuns) > 0 {
				first := sf.FailedRuns[0]
				entry.FirstSeen = &jsonFirstSeen{
					Timestamp: first.Meta.Timestamp,
					Commit:    first.Meta.Title,
					RunLink:   first.Meta.Link,
				}
			}
		}

		// Flaky info
		if fi, ok := flakyTests[testName]; ok {
			entry.FlakyInfo = &jsonFlakyInfo{
				FailCount: fi.FailCount,
				TotalRuns: fi.TotalRuns,
			}
		}

		failedTests = append(failedTests, entry)
	}

	return jsonProject{
		Repo:         r.Repo,
		Branch:       r.Branch,
		MasterBranch: cfg.Analysis.MasterBranch,
		LatestRun: jsonLatestRun{
			RunID:       latestMeta.RunID,
			SHA:         latestMeta.SHA,
			CommitTitle: latestMeta.Title,
			Timestamp:   latestMeta.Timestamp,
			Conclusion:  latestMeta.Conclusion,
			Link:        latestMeta.Link,
			TotalFailed: latestFailed.Len(),
		},
		Summary: jsonSummary{
			TotalRunsAnalyzed:  ar.Stats.TotalRuns,
			UniqueFailedTests:  ar.Stats.UniqueFailedTests,
			MasterFailedTests:  ar.Stats.MasterFailedTests,
			NewFailures:        ar.Stats.NewFailures,
			StableFailingCount: len(stableFailing),
			FixedCount:         len(fixedTests),
			FlakyCount:         len(flakyTests),
		},
		FailedTests: failedTests,
	}
}

func extractError(items []internal.TestDetail) string {
	if len(items) == 0 {
		return ""
	}
	return strings.TrimSpace(items[0].Context)
}

func extractProject(items []internal.TestDetail) string {
	if len(items) == 0 {
		return ""
	}
	return items[0].Project
}

func classifyTest(testName string, stable, fixed, flaky map[string]*analyze.TestBehavior) string {
	if _, ok := stable[testName]; ok {
		return "stable_failing"
	}
	if _, ok := fixed[testName]; ok {
		return "fixed"
	}
	if _, ok := flaky[testName]; ok {
		return "flaky"
	}
	return "single_failure"
}

// findStreakStart finds the run that started the current consecutive failure streak.
// Port of Python's _find_streak_start().
func findStreakStart(pattern string, orderedKeys []string, meta map[string]internal.RunMeta) *jsonProbableCause {
	if pattern == "" {
		return nil
	}

	// Check if last character is 🔴
	runes := []rune(pattern)
	if len(runes) == 0 {
		return nil
	}

	redCircle, _ := utf8.DecodeRuneInString("🔴")
	greenCircle, _ := utf8.DecodeRuneInString("🟢")
	_ = greenCircle

	// Count emoji positions (each emoji is one "logical" position)
	var positions []rune
	for _, r := range runes {
		if r == redCircle || r == greenCircle {
			positions = append(positions, r)
		}
	}

	if len(positions) == 0 || positions[len(positions)-1] != redCircle {
		return nil
	}

	// Walk backwards to find streak start
	idx := len(positions) - 1
	for idx > 0 && positions[idx-1] == redCircle {
		idx--
	}

	if idx >= len(orderedKeys) {
		return nil
	}

	key := orderedKeys[idx]
	runMeta := meta[key]
	return &jsonProbableCause{
		SHA:          runMeta.SHA,
		CommitTitle:  runMeta.Title,
		Timestamp:    runMeta.Timestamp,
		RunLink:      runMeta.Link,
		StreakLength: len(positions) - idx,
	}
}
