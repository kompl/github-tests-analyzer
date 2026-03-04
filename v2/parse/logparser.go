package parse

import (
	"encoding/json"
	"fmt"
	"os"
	"regexp"
	"sort"
	"strings"
)

// ParseLog parses a Ruby hash log file and extracts repo→branches mapping.
// Equivalent of Python's parse_log_to_repo_branches().
func ParseLog(logPath string, ignoreTasks []string) (map[string][]string, error) {
	data, err := os.ReadFile(logPath)
	if err != nil {
		return nil, fmt.Errorf("read log file %s: %w", logPath, err)
	}

	text := string(data)
	ignoreSet := make(map[string]struct{}, len(ignoreTasks))
	for _, t := range ignoreTasks {
		ignoreSet[t] = struct{}{}
	}

	result := make(map[string][]string)

	// Top-level project names
	projectRe := regexp.MustCompile(`(?m)^[{ ]"?([a-zA-Z][a-zA-Z0-9_-]*)"?\s*:\s*$`)
	// Version keys like "6.2.1.5" =>
	versionRe := regexp.MustCompile(`"([^"]+)"\s*=>`)
	// tasks: [...] (possibly multiline)
	tasksRe := regexp.MustCompile(`tasks:\s*\[([^\]]*)\]`)
	// Individual quoted values inside tasks array
	taskValueRe := regexp.MustCompile(`"([^"]+)"`)

	matches := projectRe.FindAllStringSubmatchIndex(text, -1)
	for i, m := range matches {
		project := text[m[2]:m[3]]
		start := m[1]
		end := len(text)
		if i+1 < len(matches) {
			end = matches[i+1][0]
		}
		section := text[start:end]

		branches := make(map[string]struct{})
		versionMatches := versionRe.FindAllStringSubmatchIndex(section, -1)
		for vi, vm := range versionMatches {
			version := section[vm[2]:vm[3]]
			vStart := vm[1]
			vEnd := len(section)
			if vi+1 < len(versionMatches) {
				vEnd = versionMatches[vi+1][0]
			}
			versionSection := section[vStart:vEnd]

			// Check tasks is non-empty and has at least one non-ignored task
			tMatch := tasksRe.FindStringSubmatch(versionSection)
			if tMatch == nil {
				continue
			}
			bracket := strings.TrimSpace(tMatch[1])
			if bracket == "" {
				continue
			}

			taskMatches := taskValueRe.FindAllStringSubmatch(bracket, -1)
			hasMeaningful := false
			for _, tm := range taskMatches {
				if _, ignored := ignoreSet[tm[1]]; !ignored {
					hasMeaningful = true
					break
				}
			}

			if hasMeaningful {
				branches[versionToBranch(version)] = struct{}{}
			}
		}

		if len(branches) > 0 {
			branchList := make([]string, 0, len(branches))
			for b := range branches {
				branchList = append(branchList, b)
			}
			sort.Strings(branchList)
			result[project] = branchList
		}
	}

	return result, nil
}

// SaveRepoBranches writes repo→branches mapping to a JSON file.
func SaveRepoBranches(path string, repoBranches map[string][]string) error {
	data, err := json.MarshalIndent(repoBranches, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal repo_branches: %w", err)
	}
	return os.WriteFile(path, data, 0644)
}

// LoadRepoBranches reads repo→branches mapping from a JSON file.
func LoadRepoBranches(path string) (map[string][]string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read repo_branches file %s: %w", path, err)
	}
	var result map[string][]string
	if err := json.Unmarshal(data, &result); err != nil {
		return nil, fmt.Errorf("parse repo_branches: %w", err)
	}
	return result, nil
}

// versionToBranch converts a version tag to a branch name.
// Rules:
//   - 4+ parts (e.g. 6.2.1.5): take first 3; if 3rd part is '0', use first 2
//   - 3 parts (e.g. 6.2.2): take first 2
//   - 2 parts (e.g. 6.3): use as-is
//
// Prefix with 'v'.
func versionToBranch(version string) string {
	parts := strings.Split(version, ".")
	switch {
	case len(parts) >= 4:
		if parts[2] == "0" {
			return "v" + parts[0] + "." + parts[1]
		}
		return "v" + parts[0] + "." + parts[1] + "." + parts[2]
	case len(parts) == 3:
		return "v" + parts[0] + "." + parts[1]
	default:
		return "v" + strings.Join(parts, ".")
	}
}
