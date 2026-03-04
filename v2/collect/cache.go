package collect

import (
	"context"
	"fmt"
	"time"

	"filedecorator/v2/internal"

	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/mongo"
	"go.mongodb.org/mongo-driver/mongo/options"
)

// cachedTestEntry matches the Python schema's details_list item.
type cachedTestEntry struct {
	TestName string                `bson:"test_name"`
	Items    []internal.TestDetail `bson:"items"`
}

// cachedDocument matches the Python schema.
type cachedDocument struct {
	Schema      int               `bson:"schema"`
	Owner       string            `bson:"owner"`
	Repo        string            `bson:"repo"`
	RunID       int               `bson:"run_id"`
	CreatedAt   string            `bson:"created_at"`
	HasNoTests  bool              `bson:"has_no_tests"`
	DetailsList []cachedTestEntry `bson:"details_list"`
}

// Cache provides MongoDB-backed storage for parsed test results.
type Cache struct {
	coll *mongo.Collection
}

// NewCache creates a Cache connected to MongoDB.
func NewCache(uri, dbName, collName string) (*Cache, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	client, err := mongo.Connect(ctx, options.Client().ApplyURI(uri))
	if err != nil {
		return nil, fmt.Errorf("connect mongo: %w", err)
	}

	if err := client.Ping(ctx, nil); err != nil {
		return nil, fmt.Errorf("ping mongo: %w", err)
	}

	coll := client.Database(dbName).Collection(collName)

	// Create unique index on (owner, repo, run_id)
	indexModel := mongo.IndexModel{
		Keys:    bson.D{{Key: "owner", Value: 1}, {Key: "repo", Value: 1}, {Key: "run_id", Value: 1}},
		Options: options.Index().SetUnique(true),
	}
	_, _ = coll.Indexes().CreateOne(ctx, indexModel)

	return &Cache{coll: coll}, nil
}

// Load retrieves cached parsed results for a run.
// Returns (details, hasNoTests, found).
func (c *Cache) Load(owner, repo string, runID int) (map[string][]internal.TestDetail, bool, bool) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	filter := bson.M{"owner": owner, "repo": repo, "run_id": runID}
	var doc cachedDocument
	err := c.coll.FindOne(ctx, filter).Decode(&doc)
	if err != nil {
		return nil, false, false
	}

	details := decodeDetails(doc.DetailsList)
	return details, doc.HasNoTests, true
}

// Save stores parsed results for a run (upsert).
func (c *Cache) Save(owner, repo string, runID int, details map[string][]internal.TestDetail, hasNoTests bool) error {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	doc := cachedDocument{
		Schema:      2,
		Owner:       owner,
		Repo:        repo,
		RunID:       runID,
		CreatedAt:   time.Now().Format(time.RFC3339),
		HasNoTests:  hasNoTests,
		DetailsList: encodeDetails(details),
	}

	filter := bson.M{"owner": owner, "repo": repo, "run_id": runID}
	opts := options.Replace().SetUpsert(true)
	_, err := c.coll.ReplaceOne(ctx, filter, doc, opts)
	if err != nil {
		return fmt.Errorf("save to mongo: %w", err)
	}
	return nil
}

// FindEarliestRunWithTests finds the earliest run_id (from candidates) where each test appears.
func (c *Cache) FindEarliestRunWithTests(owner, repo string, testNames []string, candidateRunIDs []int) map[string]StableSinceInfo {
	result := make(map[string]StableSinceInfo)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	baseFilter := bson.M{
		"owner":       owner,
		"repo":        repo,
		"has_no_tests": false,
	}
	if len(candidateRunIDs) > 0 {
		baseFilter["run_id"] = bson.M{"$in": candidateRunIDs}
	}

	for _, testName := range testNames {
		filter := bson.M{}
		for k, v := range baseFilter {
			filter[k] = v
		}
		filter["details_list.test_name"] = testName

		opts := options.FindOne().
			SetSort(bson.D{{Key: "run_id", Value: 1}}).
			SetProjection(bson.M{"run_id": 1, "created_at": 1})

		var doc struct {
			RunID     int    `bson:"run_id"`
			CreatedAt string `bson:"created_at"`
		}
		err := c.coll.FindOne(ctx, filter, opts).Decode(&doc)
		if err == nil {
			result[testName] = StableSinceInfo{
				RunID:     doc.RunID,
				CreatedAt: doc.CreatedAt,
			}
		}
	}

	return result
}

// StableSinceInfo holds the earliest run info for a stable-failing test.
type StableSinceInfo struct {
	RunID     int    `json:"run_id"`
	CreatedAt string `json:"created_at"`
}

func encodeDetails(details map[string][]internal.TestDetail) []cachedTestEntry {
	if len(details) == 0 {
		return nil
	}
	entries := make([]cachedTestEntry, 0, len(details))
	for name, items := range details {
		entries = append(entries, cachedTestEntry{
			TestName: name,
			Items:    items,
		})
	}
	return entries
}

func decodeDetails(entries []cachedTestEntry) map[string][]internal.TestDetail {
	if len(entries) == 0 {
		return nil
	}
	details := make(map[string][]internal.TestDetail, len(entries))
	for _, e := range entries {
		details[e.TestName] = e.Items
	}
	return details
}
