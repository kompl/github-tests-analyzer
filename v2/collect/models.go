package collect

import "filedecorator/v2/internal"

// CollectResult is the output of the Collect phase.
type CollectResult struct {
	Summary         map[string]internal.StringSet      // compositeKey → set of failed test names
	Meta            map[string]internal.RunMeta        // compositeKey → run metadata
	AllTestDetails  map[string][]internal.TestDetail   // testName → detail items
	MasterFailed    internal.StringSet                 // tests failing in master
	AllBranchRunIDs []int                              // ALL completed run IDs for the branch
	OrderedKeys     []string                           // composite keys in chronological order (oldest first)
}

// RunProcessResult holds the result of processing a single run.
type RunProcessResult struct {
	RunID      int
	Run        ghWorkflowRun
	Details    map[string][]internal.TestDetail
	HasNoTests bool
	Err        error
}

// ghWorkflowRun is a minimal representation of a GitHub Actions workflow run.
type ghWorkflowRun struct {
	ID           int    `json:"id"`
	Status       string `json:"status"`
	Conclusion   string `json:"conclusion"`
	HeadSHA      string `json:"head_sha"`
	HeadBranch   string `json:"head_branch"`
	RunStartedAt string `json:"run_started_at"`
	CreatedAt    string `json:"created_at"`
	HTMLURL      string `json:"html_url"`
}

// ghWorkflowRunsResponse is the GitHub API response for listing workflow runs.
type ghWorkflowRunsResponse struct {
	WorkflowRuns []ghWorkflowRun `json:"workflow_runs"`
}

// ghCommitResponse is the GitHub API response for a commit.
type ghCommitResponse struct {
	Commit struct {
		Message string `json:"message"`
	} `json:"commit"`
}

// ghArtifact represents a GitHub Actions artifact.
type ghArtifact struct {
	Name               string `json:"name"`
	Expired            bool   `json:"expired"`
	ArchiveDownloadURL string `json:"archive_download_url"`
}

// ghArtifactsResponse is the GitHub API response for listing artifacts.
type ghArtifactsResponse struct {
	Artifacts []ghArtifact `json:"artifacts"`
}
