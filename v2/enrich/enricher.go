package enrich

import (
	"fmt"

	"filedecorator/v2/analyze"
	"filedecorator/v2/collect"
	"filedecorator/v2/config"
)

// RunForRepo executes the Enrich phase for a specific repo.
func RunForRepo(cfg *config.Config, cr *collect.CollectResult, ar *analyze.AnalyzeResult, repo string) *EnrichResult {
	result := &EnrichResult{
		StableSince: make(map[string]collect.StableSinceInfo),
	}

	if len(ar.Behavior.StableFailing) == 0 {
		return result
	}

	fmt.Printf("  [enrich] Looking up history for %d stable-failing tests in MongoDB...\n", len(ar.Behavior.StableFailing))

	cache, err := collect.NewCache(cfg.Mongo.URI, cfg.Mongo.DB, cfg.Mongo.Collection)
	if err != nil {
		fmt.Printf("  [enrich] MongoDB connection error: %v\n", err)
		return result
	}

	testNames := make([]string, 0, len(ar.Behavior.StableFailing))
	for name := range ar.Behavior.StableFailing {
		testNames = append(testNames, name)
	}

	result.StableSince = cache.FindEarliestRunWithTests(
		cfg.GitHub.Owner,
		repo,
		testNames,
		cr.AllBranchRunIDs,
	)

	fmt.Printf("  [enrich] Found history for %d/%d stable-failing tests\n",
		len(result.StableSince), len(ar.Behavior.StableFailing))

	return result
}
