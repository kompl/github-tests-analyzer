package internal

// TestDetail represents a single failure detail entry for a test.
type TestDetail struct {
	File       string `json:"file" bson:"file"`
	LineNum    int    `json:"line_num" bson:"line_num"`
	Context    string `json:"context" bson:"context"`
	Project    string `json:"project" bson:"project"`
	OrderIndex int    `json:"order_index" bson:"order_index"`
}

// RunMeta holds metadata for a single workflow run.
type RunMeta struct {
	SHA          string   `json:"sha"`
	RunID        int      `json:"run_id"`
	Title        string   `json:"title"`
	Timestamp    string   `json:"ts"`
	Conclusion   string   `json:"concl"`
	Link         string   `json:"link"`
	Branch       string   `json:"branch"`
	Order        []string `json:"order"`         // test names in original parse order
	CompositeKey string   `json:"composite_key"` // "{sha}_{run_id}"
}

// StringSet is a set of strings implemented as map[string]struct{}.
type StringSet map[string]struct{}

func NewStringSet(items ...string) StringSet {
	s := make(StringSet, len(items))
	for _, item := range items {
		s[item] = struct{}{}
	}
	return s
}

func (s StringSet) Add(item string) {
	s[item] = struct{}{}
}

func (s StringSet) Contains(item string) bool {
	_, ok := s[item]
	return ok
}

func (s StringSet) Len() int {
	return len(s)
}

func (s StringSet) ToSlice() []string {
	result := make([]string, 0, len(s))
	for item := range s {
		result = append(result, item)
	}
	return result
}

func (s StringSet) Difference(other StringSet) StringSet {
	result := NewStringSet()
	for item := range s {
		if !other.Contains(item) {
			result.Add(item)
		}
	}
	return result
}

func (s StringSet) Union(other StringSet) StringSet {
	result := NewStringSet()
	for item := range s {
		result.Add(item)
	}
	for item := range other {
		result.Add(item)
	}
	return result
}
