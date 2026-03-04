package render

import (
	"encoding/json"
	"fmt"
	"html"
	"html/template"
	"os"
	"path/filepath"
	"strings"
	"time"

	"filedecorator/v2/analyze"
	"filedecorator/v2/collect"
	"filedecorator/v2/config"
	"filedecorator/v2/internal"
)

// RenderHTML generates the HTML report for a single repo/branch.
func RenderHTML(r RepoResult, cfg *config.Config) error {
	cr := r.Collect
	ar := r.Analyze

	masterBranch := cfg.Analysis.MasterBranch
	branchDir := filepath.Join(cfg.Output.Dir, strings.ReplaceAll(r.Branch, "/", "_"))
	if err := os.MkdirAll(branchDir, 0755); err != nil {
		return fmt.Errorf("mkdir %s: %w", branchDir, err)
	}
	reportPath := filepath.Join(branchDir, fmt.Sprintf("failed_tests_%s.html", r.Repo))

	// Build behavior map for leaf labels
	behaviorMap := make(map[string]*analyze.TestBehavior)
	for name, b := range ar.Behavior.StableFailing {
		behaviorMap[name] = b
	}
	for name, b := range ar.Behavior.FixedTests {
		behaviorMap[name] = b
	}
	for name, b := range ar.Behavior.FlakyTests {
		behaviorMap[name] = b
	}

	// Build template data
	data := htmlTemplateData{
		RepoName:   r.Repo,
		BranchName: r.Branch,
		ReportDate: time.Now().Format("2006-01-02 15:04:05"),
		Runs:       buildHTMLRuns(cr, ar, behaviorMap, r.Branch, masterBranch),
	}

	// Prepare test details JSON for JS
	detailsMap := make(map[string]string)
	for testPath, details := range cr.AllTestDetails {
		var sb strings.Builder
		for _, d := range details {
			fmt.Fprintf(&sb, "Файл: %s\nСтрока: %d\n\nКонтекст:\n%s\n\n---\n\n", d.File, d.LineNum, d.Context)
		}
		detailsMap[testPath] = sb.String()
	}
	detailsJSON, _ := json.Marshal(detailsMap)
	data.DetailsJSON = template.JS(strings.ReplaceAll(string(detailsJSON), "</", "<\\/"))

	// Render template
	tmplPath := filepath.Join(getTemplateDir(), "report.html.tmpl")
	tmpl, err := template.New("report.html.tmpl").Funcs(template.FuncMap{
		"safeHTML": func(s string) template.HTML { return template.HTML(s) },
	}).ParseFiles(tmplPath)
	if err != nil {
		return fmt.Errorf("parse template: %w", err)
	}

	f, err := os.Create(reportPath)
	if err != nil {
		return fmt.Errorf("create %s: %w", reportPath, err)
	}
	defer f.Close()

	if err := tmpl.Execute(f, data); err != nil {
		return fmt.Errorf("execute template: %w", err)
	}

	fmt.Printf("  [render] Generated HTML: %s\n", reportPath)
	return nil
}

func getTemplateDir() string {
	// Template is expected next to this Go source file, or in the render/ dir of the binary
	exe, _ := os.Executable()
	dir := filepath.Dir(exe)
	// Check if template exists next to executable
	tmplPath := filepath.Join(dir, "render", "report.html.tmpl")
	if _, err := os.Stat(tmplPath); err == nil {
		return filepath.Join(dir, "render")
	}
	// Fallback: relative to working directory
	return "render"
}

type htmlTemplateData struct {
	RepoName    string
	BranchName  string
	ReportDate  string
	Runs        []htmlRun
	DetailsJSON template.JS
}

type htmlRun struct {
	CommitInfo htmlCommitInfo
	Sections   []htmlSection
}

type htmlCommitInfo struct {
	Title        string
	Branch       string
	TS           string
	Conclusion   string
	Link         string
	Failed       int
	SHA          string
	CompositeKey string
}

type htmlSection struct {
	Title   string
	Total   int
	Tree    []htmlTreeNode
	MaxShow int
}

type htmlTreeNode struct {
	Name     string
	Total    int
	Children []htmlTreeNode
	Leaves   []htmlLeaf
}

type htmlLeaf struct {
	DisplayHTML template.HTML
	CleanItem   string
}

func buildHTMLRuns(
	cr *collect.CollectResult,
	ar *analyze.AnalyzeResult,
	behaviorMap map[string]*analyze.TestBehavior,
	branch, masterBranch string,
) []htmlRun {
	var runs []htmlRun

	// Behavior summary sections (stable failing, fixed, flaky)
	runs = append(runs, buildBehaviorRun("🔴 Стабильно падающие тесты", ar.Behavior.StableFailing, cr, behaviorMap, branch, masterBranch))
	runs = append(runs, buildFixedRun("✅ Починенные тесты", ar.Behavior.FixedTests))
	runs = append(runs, buildFlakyRun("🟡 Нестабильные (flaky) тесты", ar.Behavior.FlakyTests))

	// Run diff sections
	for _, diff := range ar.RunDiffs {
		meta := diff.Meta
		failedTotal := diff.Current.Len()

		commitInfo := htmlCommitInfo{
			Title:        meta.Title,
			Branch:       meta.Branch,
			TS:           meta.Timestamp,
			Conclusion:   meta.Conclusion,
			Link:         meta.Link,
			Failed:       failedTotal,
			SHA:          diff.SHA,
			CompositeKey: diff.CompositeKey,
		}

		// Build sections for this run
		addedOrdered := filterOrdered(diff.Order, diff.Added)
		removedOrdered := filterOrdered(diff.PrevOrder, diff.Removed)
		onlyHereOrdered := filterOrdered(diff.Order, diff.OnlyHere)
		allCurrentOrdered := filterOrdered(diff.Order, diff.Current)

		sections := []htmlSection{
			buildTreeSection("➕ Новые падения", addedOrdered, "added", behaviorMap, cr.MasterFailed, cr.AllTestDetails, branch, masterBranch),
			buildTreeSection("✔ Починились", removedOrdered, "removed", behaviorMap, cr.MasterFailed, cr.AllTestDetails, branch, masterBranch),
			buildTreeSection("⚠ Уникальные падения", onlyHereOrdered, "only_here", behaviorMap, cr.MasterFailed, cr.AllTestDetails, branch, masterBranch),
			buildTreeSection("📋 Все падения", allCurrentOrdered, "current", behaviorMap, cr.MasterFailed, cr.AllTestDetails, branch, masterBranch),
		}
		sections[3].MaxShow = 0 // always collapsed

		runs = append(runs, htmlRun{CommitInfo: commitInfo, Sections: sections})
	}

	return runs
}

func buildBehaviorRun(title string, tests map[string]*analyze.TestBehavior, cr *collect.CollectResult, behaviorMap map[string]*analyze.TestBehavior, branch, masterBranch string) htmlRun {
	var items []string
	for testName := range tests {
		items = append(items, testName)
	}
	section := buildTreeSection(title, items, "stable", behaviorMap, cr.MasterFailed, cr.AllTestDetails, branch, masterBranch)
	return htmlRun{
		CommitInfo: htmlCommitInfo{Title: title, TS: time.Now().Format("2006-01-02 15:04:05"), Conclusion: "unknown"},
		Sections:   []htmlSection{section},
	}
}

func buildFixedRun(title string, tests map[string]*analyze.TestBehavior) htmlRun {
	var leaves []htmlLeaf
	for testName, info := range tests {
		label := testName
		if info.NextCommitInfo != nil {
			label += fmt.Sprintf(" (починено в %s: %s)", info.NextCommitInfo.Title, info.NextPRLink)
		}
		leaves = append(leaves, htmlLeaf{
			DisplayHTML: template.HTML(html.EscapeString(label)),
			CleanItem:   testName,
		})
	}
	tree := groupIntoTree(leaves)
	return htmlRun{
		CommitInfo: htmlCommitInfo{Title: title, TS: time.Now().Format("2006-01-02 15:04:05"), Conclusion: "unknown"},
		Sections:   []htmlSection{{Title: title, Total: len(leaves), Tree: tree}},
	}
}

func buildFlakyRun(title string, tests map[string]*analyze.TestBehavior) htmlRun {
	var leaves []htmlLeaf
	for testName, info := range tests {
		failRate := 0.0
		if info.TotalRuns > 0 {
			failRate = float64(info.FailCount) / float64(info.TotalRuns) * 100
		}
		label := fmt.Sprintf("%s (паттерн: %s, падает %.1f%% времени)", testName, info.Pattern, failRate)
		leaves = append(leaves, htmlLeaf{
			DisplayHTML: template.HTML(html.EscapeString(label)),
			CleanItem:   testName,
		})
	}
	tree := groupIntoTree(leaves)
	return htmlRun{
		CommitInfo: htmlCommitInfo{Title: title, TS: time.Now().Format("2006-01-02 15:04:05"), Conclusion: "unknown"},
		Sections:   []htmlSection{{Title: title, Total: len(leaves), Tree: tree}},
	}
}

func buildTreeSection(
	title string,
	items []string,
	section string,
	behaviorMap map[string]*analyze.TestBehavior,
	masterFailed internal.StringSet,
	allTestDetails map[string][]internal.TestDetail,
	branch, masterBranch string,
) htmlSection {
	var leaves []htmlLeaf
	for _, t := range items {
		leaf := buildLeafLabel(t, section, behaviorMap, masterFailed, branch, masterBranch)
		leaves = append(leaves, leaf)
	}
	tree := groupIntoTree(leaves)
	return htmlSection{
		Title:   title,
		Total:   len(items),
		Tree:    tree,
		MaxShow: 10, // Python default: max_show=10; only "Все падения" overrides to 0
	}
}

// buildLeafLabel builds display label for a test.
// Port of Python's build_leaf_label() from report_service.py lines 188-216.
func buildLeafLabel(
	testName, section string,
	behaviorMap map[string]*analyze.TestBehavior,
	masterFailed internal.StringSet,
	branch, masterBranch string,
) htmlLeaf {
	marker := ""
	if (section == "added" || section == "only_here" || section == "stable") && branch != masterBranch {
		if masterFailed.Contains(testName) {
			marker = " (также в master)"
		} else {
			marker = " (только здесь)"
		}
	}

	binfo := behaviorMap[testName]
	var ts, title string
	var anchorKey string
	badgeHTML := ""

	if binfo != nil {
		switch binfo.Type {
		case "stable_failing":
			badgeHTML = " 🔴"
		case "flaky":
			badgeHTML = " 🟡"
		}
		if len(binfo.FailedRuns) > 0 {
			firstFail := binfo.FailedRuns[0]
			ts = firstFail.Meta.Timestamp
			title = firstFail.Meta.Title
			anchorKey = firstFail.CompositeKey
		}
	}

	// Leaf name: last segment after ::
	leafName := testName
	if idx := strings.LastIndex(testName, "::"); idx >= 0 {
		leafName = testName[idx+2:]
	}

	var labelText string
	if ts != "" || title != "" {
		labelText = fmt.Sprintf("%s%s — с %s — %s", leafName, marker, ts, title)
	} else {
		labelText = leafName + marker
	}

	labelSafe := html.EscapeString(labelText)
	buttonHTML := ""
	if anchorKey != "" {
		buttonHTML = fmt.Sprintf(` <button onclick="scrollToRun('run-%s')">К запуску</button>`, anchorKey)
	}

	return htmlLeaf{
		DisplayHTML: template.HTML(labelSafe + badgeHTML + buttonHTML),
		CleanItem:   testName,
	}
}

// groupIntoTree groups leaves by :: delimiters into a tree.
// Port of Python's tree building in html.py:add_run_section().
func groupIntoTree(leaves []htmlLeaf) []htmlTreeNode {
	if len(leaves) == 0 {
		return nil
	}

	type nodeRef struct {
		node *htmlTreeNode
	}

	root := make([]htmlTreeNode, 0)

	findOrCreate := func(nodes *[]htmlTreeNode, name string) *htmlTreeNode {
		for i := range *nodes {
			if (*nodes)[i].Name == name {
				return &(*nodes)[i]
			}
		}
		*nodes = append(*nodes, htmlTreeNode{Name: name})
		return &(*nodes)[len(*nodes)-1]
	}

	defaultGroup := "Без префикса"

	for _, leaf := range leaves {
		raw := leaf.CleanItem
		// Split by ' — ' first, take base
		base := raw
		if idx := strings.Index(raw, " — "); idx >= 0 {
			base = raw[:idx]
		}
		// Remove ' (' suffix
		if idx := strings.Index(base, " ("); idx >= 0 {
			base = base[:idx]
		}

		var parts []string
		if strings.Contains(base, "::") {
			parts = strings.Split(base, "::")
		} else {
			parts = []string{base}
		}

		// Parent parts are all but last; if only one segment, use default group
		var parentParts []string
		if len(parts) > 1 {
			parentParts = parts[:len(parts)-1]
		} else {
			parentParts = []string{defaultGroup}
		}

		// Navigate/create tree path
		nodes := &root
		var lastNode *htmlTreeNode
		for _, part := range parentParts {
			lastNode = findOrCreate(nodes, part)
			nodes = &lastNode.Children
		}
		if lastNode != nil {
			lastNode.Leaves = append(lastNode.Leaves, leaf)
		}
	}

	// Compute totals
	var computeTotal func(nodes []htmlTreeNode) int
	computeTotal = func(nodes []htmlTreeNode) int {
		total := 0
		for i := range nodes {
			childTotal := computeTotal(nodes[i].Children)
			nodes[i].Total = childTotal + len(nodes[i].Leaves)
			total += nodes[i].Total
		}
		return total
	}
	computeTotal(root)

	return root
}

func filterOrdered(order []string, set internal.StringSet) []string {
	var result []string
	for _, t := range order {
		if set.Contains(t) {
			result = append(result, t)
		}
	}
	return result
}
