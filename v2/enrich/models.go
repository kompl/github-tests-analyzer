package enrich

import "filedecorator/v2/collect"

// EnrichResult is the output of the Enrich phase.
type EnrichResult struct {
	StableSince map[string]collect.StableSinceInfo // testName → earliest run info
}
