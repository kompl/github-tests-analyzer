package config

import (
	"fmt"
	"os"

	"gopkg.in/yaml.v3"
)

type GitHubConfig struct {
	Owner        string `yaml:"owner"`
	WorkflowFile string `yaml:"workflow_file"`
}

type MongoConfig struct {
	URI        string `yaml:"uri"`
	DB         string `yaml:"db"`
	Collection string `yaml:"collection"`
}

type AnalysisConfig struct {
	MasterBranch string   `yaml:"master_branch"`
	MaxRuns      int      `yaml:"max_runs"`
	IgnoreTasks  []string `yaml:"ignore_tasks"`
}

type OutputConfig struct {
	Dir               string `yaml:"dir"`
	SaveLogs          bool   `yaml:"save_logs"`
	ForceRefreshCache bool   `yaml:"force_refresh_cache"`
	GenerateJSON      bool   `yaml:"generate_json"`
}

type InputConfig struct {
	LogFile          string `yaml:"log_file"`
	RepoBranchesFile string `yaml:"repo_branches_file"`
}

type Config struct {
	GitHub   GitHubConfig   `yaml:"github"`
	Mongo    MongoConfig    `yaml:"mongo"`
	Analysis AnalysisConfig `yaml:"analysis"`
	Output   OutputConfig   `yaml:"output"`
	Phases   []string       `yaml:"phases"`
	Input    InputConfig    `yaml:"input"`
}

func Load(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read config %s: %w", path, err)
	}

	cfg := &Config{
		// Defaults
		GitHub: GitHubConfig{
			Owner:        "hydra-billing",
			WorkflowFile: "ci.yml",
		},
		Mongo: MongoConfig{
			URI:        "mongodb://root:example@localhost:27017",
			DB:         "filedecorator_v2",
			Collection: "parsed_results",
		},
		Analysis: AnalysisConfig{
			MasterBranch: "master",
			MaxRuns:      100,
		},
		Output: OutputConfig{
			Dir:          "downloaded_logs",
			GenerateJSON: true,
		},
		Phases: []string{"parse", "collect", "analyze", "enrich", "render"},
		Input: InputConfig{
			LogFile:          "1.log",
			RepoBranchesFile: "repo_branches.json",
		},
	}

	if err := yaml.Unmarshal(data, cfg); err != nil {
		return nil, fmt.Errorf("parse config %s: %w", path, err)
	}

	return cfg, nil
}

func (c *Config) HasPhase(name string) bool {
	for _, p := range c.Phases {
		if p == name {
			return true
		}
	}
	return false
}
