package analyze

import "filedecorator/v2/internal"

// AnalyzeResult is the output of the Analyze phase.
type AnalyzeResult struct {
	Behavior BehaviorAnalysis
	RunDiffs []RunDiff
	Stats    Stats
}

// BehaviorAnalysis contains classified test behaviors.
type BehaviorAnalysis struct {
	StableFailing map[string]*TestBehavior
	FixedTests    map[string]*TestBehavior
	FlakyTests    map[string]*TestBehavior
}

// TestBehavior describes the behavior pattern of a single test.
type TestBehavior struct {
	Type           string           `json:"type"` // stable_failing, fixed, flaky, single_failure
	TestName       string           `json:"test_name"`
	TotalRuns      int              `json:"total_runs"`
	FailCount      int              `json:"fail_count"`
	FirstFailRun   *int             `json:"first_fail_run"`  // 1-based, nil if never failed
	LastFailRun    *int             `json:"last_fail_run"`   // 1-based, nil if never failed
	FailedRuns     []FailedRunInfo  `json:"failed_runs"`
	Pattern        string           `json:"pattern"` // e.g. "🔴🟢🔴🔴"
	Details        []internal.TestDetail `json:"details"`
	NextPRLink     string           `json:"next_pr_link,omitempty"`
	NextCommitInfo *CommitInfo      `json:"next_commit_info,omitempty"`
}

// FailedRunInfo contains info about a single failed run for a test.
type FailedRunInfo struct {
	SHA          string           `json:"sha"`
	CompositeKey string           `json:"composite_key"`
	Meta         internal.RunMeta `json:"meta"`
	RunNumber    int              `json:"run_number"` // 1-based
}

// CommitInfo for the commit that fixed a test.
type CommitInfo struct {
	SHA   string `json:"sha"`
	Title string `json:"title"`
	TS    string `json:"ts"`
	Link  string `json:"link"`
}

// RunDiff represents the difference in failed tests between two consecutive runs.
type RunDiff struct {
	SHA          string            `json:"sha"`
	CompositeKey string            `json:"composite_key"`
	Meta         internal.RunMeta  `json:"meta"`
	Order        []string          `json:"order"`
	PrevOrder    []string          `json:"prev_order"`
	Added        internal.StringSet `json:"added"`
	Removed      internal.StringSet `json:"removed"`
	OnlyHere     internal.StringSet `json:"only_here"`
	Current      internal.StringSet `json:"current"`
}

// Stats contains aggregate statistics from the analysis.
type Stats struct {
	TotalRuns         int `json:"total_runs"`
	UniqueFailedTests int `json:"unique_failed_tests"`
	MasterFailedTests int `json:"master_failed_tests"`
	NewFailures       int `json:"new_failures"`
}
