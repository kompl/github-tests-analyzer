package collect

import (
	"context"
	"fmt"
	"log"
	"math"
	"sort"
	"strings"
	"time"

	"filedecorator/v2/config"
	"filedecorator/v2/internal"
)

const maxPages = 10

// Run executes the Collect phase for a single repo/branch.
// onProgress is called each time a valid run is collected (may be nil).
func Run(token string, cfg *config.Config, repo, branch string, onProgress func()) *CollectResult {
	gh := NewGitHubClient(token, cfg.GitHub.Owner, cfg.GitHub.WorkflowFile)

	cache, err := NewCache(cfg.Mongo.URI, cfg.Mongo.DB, cfg.Mongo.Collection)
	if err != nil {
		log.Printf("[collect] MongoDB connection error: %v", err)
		return nil
	}

	logExtractor := NewLogExtractor()
	artifactExtractor := NewArtifactExtractor(gh)

	forceRefresh := cfg.Output.ForceRefreshCache
	owner := cfg.GitHub.Owner

	// --- Master failed tests ---
	masterFailed := internal.NewStringSet()
	if branch != cfg.Analysis.MasterBranch {
		fmt.Printf("  [collect] Looking for failed tests in '%s'...\n", cfg.Analysis.MasterBranch)
		masterFailed = getMasterFailed(gh, cache, logExtractor, artifactExtractor, owner, repo, cfg.Analysis.MasterBranch, forceRefresh)
		fmt.Printf("  [collect] Found %d failing tests in %s\n", masterFailed.Len(), cfg.Analysis.MasterBranch)
	}

	// --- Collect valid runs with early stopping ---
	fmt.Printf("  [collect] Collecting up to %d valid runs for %s/%s...\n", cfg.Analysis.MaxRuns, repo, branch)
	validRuns, allBranchRunIDs := collectValidRuns(
		gh, cache, logExtractor, artifactExtractor,
		owner, repo, branch, cfg.Analysis.MaxRuns, forceRefresh, onProgress,
	)

	if len(validRuns) == 0 {
		fmt.Println("  [collect] No valid runs with test results found")
		return nil
	}

	// Reverse to chronological order (oldest first)
	for i, j := 0, len(validRuns)-1; i < j; i, j = i+1, j-1 {
		validRuns[i], validRuns[j] = validRuns[j], validRuns[i]
	}

	// --- Build summary, meta, allTestDetails ---
	result := buildSummary(gh, owner, repo, validRuns)
	result.MasterFailed = masterFailed
	result.AllBranchRunIDs = allBranchRunIDs

	fmt.Printf("  [collect] Built summary: %d runs, %d unique tests\n", len(result.OrderedKeys), len(result.AllTestDetails))
	return result
}

type processedRun struct {
	run     ghWorkflowRun
	details map[string][]internal.TestDetail
}

// candidateRun wraps a workflow run with its global ordering index.
type candidateRun struct {
	index int
	run   ghWorkflowRun
}

// runResult is the outcome of processing a single candidate.
type runResult struct {
	candidate candidateRun
	details   map[string][]internal.TestDetail
	valid     bool
}

// collectValidRuns paginates GitHub API and processes runs in parallel,
// stopping as soon as maxRuns valid runs are collected.
//
// Architecture:
//   - Paginator goroutine fetches pages and sends completed runs as candidates.
//   - Orchestrator launches maxRuns workers initially.
//   - For each invalid result, a replacement worker is launched for the next candidate.
//   - This keeps maxRuns workers always in flight (no idle time).
//   - Paginator continues collecting all branch run IDs even after enough valid runs are found.
func collectValidRuns(
	gh *GitHubClient, cache *Cache,
	logExt *LogExtractor, artExt *ArtifactExtractor,
	owner, repo, branch string,
	maxRuns int,
	forceRefresh bool,
	onProgress func(),
) ([]processedRun, []int) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	candidateCh := make(chan candidateRun, 100)
	allRunIDsCh := make(chan []int, 1)

	// Paginator goroutine: fetches all pages, collects run IDs,
	// sends candidates until cancelled.
	go func() {
		defer close(candidateCh)
		var allIDs []int
		defer func() { allRunIDsCh <- allIDs }()

		cancelled := false
		idx := 0
		for page := 1; page <= maxPages; page++ {
			runs, err := gh.FetchRunsPage(repo, branch, page)
			if err != nil {
				log.Printf("[collect] Error listing runs page %d: %v", page, err)
				break
			}
			if len(runs) == 0 {
				break
			}

			for _, run := range runs {
				if run.Status != "completed" || (run.Conclusion != "success" && run.Conclusion != "failure") {
					continue
				}
				allIDs = append(allIDs, run.ID)
				if !cancelled {
					select {
					case candidateCh <- candidateRun{index: idx, run: run}:
						idx++
					case <-ctx.Done():
						cancelled = true
					}
				}
			}
		}
	}()

	resultCh := make(chan runResult, maxRuns)

	// Phase 1: launch up to maxRuns workers
	inFlight := 0
	for i := 0; i < maxRuns; i++ {
		c, ok := <-candidateCh
		if !ok {
			break
		}
		inFlight++
		go processCandidate(cache, logExt, artExt, gh, owner, repo, c, forceRefresh, resultCh)
	}

	// Phase 2: collect results, replace invalid runs with next candidates
	var validResults []runResult
	for inFlight > 0 && len(validResults) < maxRuns {
		r := <-resultCh
		inFlight--

		if r.valid {
			validResults = append(validResults, r)
			if onProgress != nil {
				onProgress()
			}
			fmt.Printf("  [collect] Run %d valid (%d/%d)\n", r.candidate.run.ID, len(validResults), maxRuns)
		} else {
			fmt.Printf("  [collect] Run %d has no test results, replacing\n", r.candidate.run.ID)
			c, ok := <-candidateCh
			if ok {
				inFlight++
				go processCandidate(cache, logExt, artExt, gh, owner, repo, c, forceRefresh, resultCh)
			}
		}
	}

	// Stop sending candidates; paginator continues collecting IDs from remaining pages
	cancel()
	allRunIDs := <-allRunIDsCh

	// Sort valid results by original API order (newest first)
	sort.Slice(validResults, func(i, j int) bool {
		return validResults[i].candidate.index < validResults[j].candidate.index
	})

	runs := make([]processedRun, len(validResults))
	for i, r := range validResults {
		runs[i] = processedRun{run: r.candidate.run, details: r.details}
	}

	fmt.Printf("  [collect] Collected %d valid runs, %d total branch run IDs\n", len(runs), len(allRunIDs))
	return runs, allRunIDs
}

func processCandidate(
	cache *Cache, logExt *LogExtractor, artExt *ArtifactExtractor,
	gh *GitHubClient, owner, repo string,
	c candidateRun, forceRefresh bool,
	resultCh chan<- runResult,
) {
	details, hasNoTests := loadOrExtract(cache, logExt, artExt, gh, owner, repo, c.run.ID, forceRefresh)
	resultCh <- runResult{candidate: c, details: details, valid: !hasNoTests}
}

func loadOrExtract(
	cache *Cache, logExt *LogExtractor, artExt *ArtifactExtractor,
	gh *GitHubClient, owner, repo string, runID int, forceRefresh bool,
) (map[string][]internal.TestDetail, bool) {
	// 1. Check cache
	if !forceRefresh {
		details, hasNoTests, found := cache.Load(owner, repo, runID)
		if found {
			if !hasNoTests {
				fmt.Printf("  [collect] Cache hit for run %d\n", runID)
				return details, false
			}
			fmt.Printf("  [collect] Cache invalid for run %d (has_no_tests=true), re-extracting\n", runID)
		}
	}

	// 2. Try artifacts first
	details, hasNoTests := artExt.Extract(repo, runID)
	isValid := !hasNoTests || len(details) > 0

	// 3. Fallback to logs
	if !isValid {
		fmt.Printf("  [collect] Fallback to logs for run %d\n", runID)
		logBytes, err := gh.DownloadLogs(repo, runID)
		if err == nil && len(logBytes) > 0 {
			altDetails, altHasNoTests := logExt.ParseZip(logBytes)
			if !altHasNoTests {
				details = altDetails
				hasNoTests = altHasNoTests
			}
		}
	}

	// 4. Save to cache
	if err := cache.Save(owner, repo, runID, details, hasNoTests); err != nil {
		log.Printf("[collect] Error saving to cache for run %d: %v", runID, err)
	}

	return details, hasNoTests
}

func getMasterFailed(
	gh *GitHubClient, cache *Cache,
	logExt *LogExtractor, artExt *ArtifactExtractor,
	owner, repo, masterBranch string, forceRefresh bool,
) internal.StringSet {
	run := gh.GetLatestCompletedRun(repo, masterBranch)
	if run == nil {
		return internal.NewStringSet()
	}

	details, hasNoTests := loadOrExtract(cache, logExt, artExt, gh, owner, repo, run.ID, forceRefresh)
	if hasNoTests || len(details) == 0 {
		return internal.NewStringSet()
	}

	result := internal.NewStringSet()
	for testName := range details {
		result.Add(testName)
	}
	return result
}

func buildSummary(gh *GitHubClient, owner, repo string, runs []processedRun) *CollectResult {
	result := &CollectResult{
		Summary:        make(map[string]internal.StringSet),
		Meta:           make(map[string]internal.RunMeta),
		AllTestDetails: make(map[string][]internal.TestDetail),
	}

	var orderedKeys []string

	for _, pr := range runs {
		run := pr.run
		sha := run.HeadSHA
		runID := run.ID
		compositeKey := fmt.Sprintf("%s_%d", sha, runID)

		title := gh.GetCommitTitle(repo, sha)
		ts := parseTimestamp(run.RunStartedAt, run.CreatedAt)
		link := gh.RunURL(repo, runID)

		// Build ordered list of test names, sorted by order_index
		// (Python dict preserves insertion order; Go map does not,
		// so we reconstruct order from the order_index in TestDetail)
		failedOrder := make([]string, 0, len(pr.details))
		for testName := range pr.details {
			failedOrder = append(failedOrder, testName)
		}
		sort.Slice(failedOrder, func(i, j int) bool {
			oi, oj := math.MaxInt, math.MaxInt
			if items := pr.details[failedOrder[i]]; len(items) > 0 {
				oi = items[0].OrderIndex
			}
			if items := pr.details[failedOrder[j]]; len(items) > 0 {
				oj = items[0].OrderIndex
			}
			return oi < oj
		})

		failedSet := internal.NewStringSet(failedOrder...)

		// Merge test details
		for testName, items := range pr.details {
			result.AllTestDetails[testName] = items
		}

		result.Summary[compositeKey] = failedSet
		result.Meta[compositeKey] = internal.RunMeta{
			SHA:          sha,
			RunID:        runID,
			Title:        title,
			Timestamp:    ts,
			Conclusion:   run.Conclusion,
			Link:         link,
			Branch:       run.HeadBranch,
			Order:        failedOrder,
			CompositeKey: compositeKey,
		}
		orderedKeys = append(orderedKeys, compositeKey)
	}

	result.OrderedKeys = orderedKeys
	return result
}

func parseTimestamp(runStartedAt, createdAt string) string {
	raw := runStartedAt
	if raw == "" {
		raw = createdAt
	}
	raw = strings.Replace(raw, "Z", "+00:00", 1)

	t, err := time.Parse(time.RFC3339, raw)
	if err != nil {
		// Try alternate format
		t, err = time.Parse("2006-01-02T15:04:05+00:00", raw)
		if err != nil {
			return raw
		}
	}
	return t.Format("2006-01-02 15:04:05")
}
