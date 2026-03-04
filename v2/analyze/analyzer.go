package analyze

import (
	"fmt"
	"strings"

	"filedecorator/v2/collect"
	"filedecorator/v2/internal"
)

// Run executes the Analyze phase. Pure computation, no I/O.
// Exact port of Python's TestAnalysisResults methods.
func Run(cr *collect.CollectResult) *AnalyzeResult {
	behavior := analyzeTestBehavior(cr)
	diffs := getRunDiffs(cr)
	stats := getStatistics(cr)

	fmt.Printf("  [analyze] Results: %d stable failing, %d fixed, %d flaky\n",
		len(behavior.StableFailing), len(behavior.FixedTests), len(behavior.FlakyTests))

	return &AnalyzeResult{
		Behavior: behavior,
		RunDiffs: diffs,
		Stats:    stats,
	}
}

// analyzeTestBehavior builds a state matrix for each test across all runs
// and classifies behavior. Exact port of Python's analyze_test_behavior().
func analyzeTestBehavior(cr *collect.CollectResult) BehaviorAnalysis {
	if len(cr.Summary) == 0 {
		return BehaviorAnalysis{
			StableFailing: make(map[string]*TestBehavior),
			FixedTests:    make(map[string]*TestBehavior),
			FlakyTests:    make(map[string]*TestBehavior),
		}
	}

	// Collect all unique test names
	allTests := internal.NewStringSet()
	for _, failedSet := range cr.Summary {
		for t := range failedSet {
			allTests.Add(t)
		}
	}

	orderedKeys := cr.OrderedKeys

	fmt.Printf("  [analyze] Analyzing behavior of %d unique tests across %d runs\n", allTests.Len(), len(orderedKeys))

	// Build state matrix: test → [failed_in_run_0, failed_in_run_1, ...]
	testStates := make(map[string][]bool)
	for t := range allTests {
		states := make([]bool, len(orderedKeys))
		for i, key := range orderedKeys {
			states[i] = cr.Summary[key].Contains(t)
		}
		testStates[t] = states
	}

	stableFailing := make(map[string]*TestBehavior)
	fixedTests := make(map[string]*TestBehavior)
	flakyTests := make(map[string]*TestBehavior)

	for test, states := range testStates {
		behavior := analyzeTestPattern(test, states, orderedKeys, cr)
		switch behavior.Type {
		case "stable_failing":
			stableFailing[test] = behavior
		case "fixed":
			fixedTests[test] = behavior
		case "flaky":
			flakyTests[test] = behavior
		}
		// single_failure and never_failed are not stored
	}

	return BehaviorAnalysis{
		StableFailing: stableFailing,
		FixedTests:    fixedTests,
		FlakyTests:    flakyTests,
	}
}

// analyzeTestPattern determines the behavior type of a single test.
// Exact port of Python's _analyze_test_pattern().
func analyzeTestPattern(testName string, states []bool, compositeKeys []string, cr *collect.CollectResult) *TestBehavior {
	var firstFailIdx, lastFailIdx *int
	failCount := 0

	for i, isFailed := range states {
		if isFailed {
			if firstFailIdx == nil {
				idx := i
				firstFailIdx = &idx
			}
			idx := i
			lastFailIdx = &idx
			failCount++
		}
	}

	if failCount == 0 {
		return &TestBehavior{Type: "never_failed"}
	}

	totalRuns := len(states)

	// Determine behavior type
	var behaviorType string
	if failCount == 1 {
		behaviorType = "single_failure"
	} else if *firstFailIdx == *lastFailIdx {
		behaviorType = "single_failure"
	} else if *lastFailIdx == totalRuns-1 {
		if isStableFailingFrom(states, *firstFailIdx) {
			behaviorType = "stable_failing"
		} else {
			behaviorType = "flaky"
		}
	} else {
		if hasFlakyBehavior(states) {
			behaviorType = "flaky"
		} else {
			behaviorType = "fixed"
		}
	}

	// Collect failed run info
	var failedRuns []FailedRunInfo
	for i, isFailed := range states {
		if !isFailed {
			continue
		}
		key := compositeKeys[i]
		meta := cr.Meta[key]
		sha := meta.SHA
		if sha == "" {
			parts := strings.SplitN(key, "_", 2)
			if len(parts) > 0 {
				sha = parts[0]
			}
		}
		failedRuns = append(failedRuns, FailedRunInfo{
			SHA:          sha,
			CompositeKey: key,
			Meta:         meta,
			RunNumber:    i + 1,
		})
	}

	// Find PR/commit info after last failed run
	var nextPRLink string
	var nextCommitInfo *CommitInfo
	if lastFailIdx != nil && *lastFailIdx+1 < totalRuns {
		nextKey := compositeKeys[*lastFailIdx+1]
		nextMeta := cr.Meta[nextKey]
		nextPRLink = nextMeta.Link
		sha := nextMeta.SHA
		if sha == "" {
			parts := strings.SplitN(nextKey, "_", 2)
			if len(parts) > 0 {
				sha = parts[0]
			}
		}
		nextCommitInfo = &CommitInfo{
			SHA:   sha[:min(len(sha), 7)],
			Title: nextMeta.Title,
			TS:    nextMeta.Timestamp,
			Link:  nextPRLink,
		}
	}

	// Build pattern string
	var patternBuilder strings.Builder
	for _, s := range states {
		if s {
			patternBuilder.WriteString("🔴")
		} else {
			patternBuilder.WriteString("🟢")
		}
	}

	// 1-based run numbers
	var firstRun, lastRun *int
	if firstFailIdx != nil {
		v := *firstFailIdx + 1
		firstRun = &v
	}
	if lastFailIdx != nil {
		v := *lastFailIdx + 1
		lastRun = &v
	}

	details := cr.AllTestDetails[testName]

	return &TestBehavior{
		Type:           behaviorType,
		TestName:       testName,
		TotalRuns:      totalRuns,
		FailCount:      failCount,
		FirstFailRun:   firstRun,
		LastFailRun:    lastRun,
		FailedRuns:     failedRuns,
		Pattern:        patternBuilder.String(),
		Details:        details,
		NextPRLink:     nextPRLink,
		NextCommitInfo: nextCommitInfo,
	}
}

// isStableFailingFrom checks if the test fails in all runs from startIdx to the end.
// Exact port of Python's _is_stable_failing_from().
func isStableFailingFrom(states []bool, startIdx int) bool {
	if startIdx >= len(states) {
		return false
	}
	for i := startIdx; i < len(states); i++ {
		if !states[i] {
			return false
		}
	}
	return true
}

// hasFlakyBehavior checks if a test has alternating pass/fail pattern.
// Exact port of Python's _has_flaky_behavior().
func hasFlakyBehavior(states []bool) bool {
	if len(states) < 2 {
		return false
	}
	transitions := 0
	for i := 1; i < len(states); i++ {
		if states[i] != states[i-1] {
			transitions++
		}
	}
	return transitions > 2
}

// getRunDiffs computes the diff between consecutive runs.
// Exact port of Python's get_run_diffs().
func getRunDiffs(cr *collect.CollectResult) []RunDiff {
	var diffs []RunDiff
	prev := internal.NewStringSet()
	var prevKey string

	for _, compositeKey := range cr.OrderedKeys {
		curr := cr.Summary[compositeKey]
		added := curr.Difference(prev)
		removed := prev.Difference(curr)

		onlyHere := internal.NewStringSet()
		if cr.MasterFailed.Len() > 0 {
			onlyHere = curr.Difference(cr.MasterFailed)
		}

		meta := cr.Meta[compositeKey]
		sha := meta.SHA
		if sha == "" {
			parts := strings.SplitN(compositeKey, "_", 2)
			if len(parts) > 0 {
				sha = parts[0]
			}
		}

		var prevOrder []string
		if prevKey != "" {
			prevOrder = cr.Meta[prevKey].Order
		}

		diffs = append(diffs, RunDiff{
			SHA:          sha,
			CompositeKey: compositeKey,
			Meta:         meta,
			Order:        meta.Order,
			PrevOrder:    prevOrder,
			Added:        added,
			Removed:      removed,
			OnlyHere:     onlyHere,
			Current:      curr,
		})

		prev = curr
		prevKey = compositeKey
	}

	return diffs
}

// getStatistics computes aggregate statistics.
// Exact port of Python's get_statistics().
func getStatistics(cr *collect.CollectResult) Stats {
	if len(cr.Summary) == 0 {
		return Stats{}
	}

	allFailed := internal.NewStringSet()
	for _, failedSet := range cr.Summary {
		allFailed = allFailed.Union(failedSet)
	}

	newFailures := 0
	if cr.MasterFailed.Len() > 0 {
		newFailures = allFailed.Difference(cr.MasterFailed).Len()
	}

	return Stats{
		TotalRuns:         len(cr.Summary),
		UniqueFailedTests: allFailed.Len(),
		MasterFailedTests: cr.MasterFailed.Len(),
		NewFailures:       newFailures,
	}
}
