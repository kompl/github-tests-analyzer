package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"
	"strings"
	"sync"

	"github.com/vbauerster/mpb/v8"
	"github.com/vbauerster/mpb/v8/decor"

	"filedecorator/v2/analyze"
	"filedecorator/v2/collect"
	"filedecorator/v2/config"
	"filedecorator/v2/enrich"
	"filedecorator/v2/parse"
	"filedecorator/v2/render"
)

// phaseState tracks progress for a single repo/branch.
type phaseState struct {
	mu        sync.Mutex
	phase     string
	collected int
	maxRuns   int
}

func (s *phaseState) set(phase string) {
	s.mu.Lock()
	s.phase = phase
	s.mu.Unlock()
}

func (s *phaseState) incr() {
	s.mu.Lock()
	s.collected++
	s.mu.Unlock()
}

func (s *phaseState) render() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	switch s.phase {
	case "collect":
		return s.collectBar()
	case "analyze":
		return " Analyze"
	case "enrich":
		return " Enrich"
	case "done":
		return " ✓"
	case "nodata":
		return " — нет данных"
	}
	return ""
}

func (s *phaseState) collectBar() string {
	const w = 30
	filled := 0
	if s.maxRuns > 0 {
		filled = s.collected * w / s.maxRuns
	}
	if filled > w {
		filled = w
	}
	tip := ""
	pad := w - filled
	if filled < w && s.collected > 0 {
		tip = ">"
		pad--
	}
	return fmt.Sprintf(" [%s%s%s] Collect %d/%d",
		strings.Repeat("=", filled), tip, strings.Repeat(" ", pad),
		s.collected, s.maxRuns)
}

func main() {
	configPath := flag.String("config", "config.yaml", "path to config file")
	flag.Parse()

	cfg, err := config.Load(*configPath)
	if err != nil {
		log.Fatalf("Failed to load config: %v", err)
	}

	token := os.Getenv("GITHUB_TOKEN")
	if token == "" {
		log.Fatal("GITHUB_TOKEN env var is required")
	}

	// Ensure output directory exists
	if err := os.MkdirAll(cfg.Output.Dir, 0755); err != nil {
		log.Fatalf("Failed to create output dir: %v", err)
	}

	// ========== Parse phase ==========
	var repoBranches map[string][]string

	if cfg.HasPhase("parse") {
		fmt.Println("\n=== Parse: Парсинг лога → repo_branches.json ===")
		repoBranches, err = parse.ParseLog(cfg.Input.LogFile, cfg.Analysis.IgnoreTasks)
		if err != nil {
			log.Fatalf("Parse phase failed: %v", err)
		}

		if len(repoBranches) > 0 {
			if err := parse.SaveRepoBranches(cfg.Input.RepoBranchesFile, repoBranches); err != nil {
				log.Fatalf("Failed to save repo_branches: %v", err)
			}
			fmt.Printf("  Собрано %d проектов, сохранено в %s\n", len(repoBranches), cfg.Input.RepoBranchesFile)
			data, _ := json.MarshalIndent(repoBranches, "  ", "  ")
			fmt.Printf("  %s\n", string(data))
		} else {
			fmt.Println("  Не удалось извлечь данные из лога")
		}
		fmt.Println("=== Parse завершена ===")
	}

	// Load repo_branches from file (in case Parse phase was skipped)
	if repoBranches == nil {
		repoBranches, err = parse.LoadRepoBranches(cfg.Input.RepoBranchesFile)
		if err != nil {
			log.Fatalf("Failed to load repo_branches: %v", err)
		}
	}

	if len(repoBranches) == 0 {
		fmt.Println("Нет проектов для анализа")
		return
	}

	// ========== Collect → Analyze → Enrich per repo/branch ==========
	needCollect := cfg.HasPhase("collect")
	needAnalyze := cfg.HasPhase("analyze")
	needEnrich := cfg.HasPhase("enrich")
	needRender := cfg.HasPhase("render")

	if !needCollect {
		fmt.Println("⏩ Фазы collect/analyze/enrich/render пропущены")
		return
	}

	fmt.Println("\n=== Collect → Analyze → Enrich ===")

	// Calculate max name width for alignment
	nameWidth := 0
	for repo, branches := range repoBranches {
		for _, branch := range branches {
			if n := len(repo) + 1 + len(branch); n > nameWidth {
				nameWidth = n
			}
		}
	}

	maxRuns := cfg.Analysis.MaxRuns

	// Suppress verbose stdout during progress bar display;
	// capture log (stderr) messages in a buffer to show after.
	origStdout := os.Stdout
	devNull, _ := os.OpenFile(os.DevNull, os.O_WRONLY, 0)
	os.Stdout = devNull

	var logBuf bytes.Buffer
	log.SetOutput(&logBuf)

	p := mpb.New(mpb.WithOutput(origStdout))

	resultCh := make(chan render.RepoResult, 64)
	var wg sync.WaitGroup

	for repo, branches := range repoBranches {
		for _, branch := range branches {
			name := fmt.Sprintf("%s/%s", repo, branch)
			state := &phaseState{phase: "collect", maxRuns: maxRuns}

			spinner := p.New(0,
				mpb.SpinnerStyle(),
				mpb.BarWidth(1),
				mpb.BarFillerClearOnComplete(),
				mpb.PrependDecorators(
					decor.Name(name, decor.WC{W: nameWidth + 2, C: decor.DindentRight}),
				),
				mpb.AppendDecorators(
					decor.Any(func(s decor.Statistics) string {
						return state.render()
					}),
				),
			)

			wg.Add(1)
			go func(repo, branch string, spinner *mpb.Bar, state *phaseState) {
				defer wg.Done()

				// Collect — spinner animates, text shows progress bar
				cr := collect.Run(token, cfg, repo, branch, func() {
					state.incr()
				})

				if cr == nil {
					state.set("nodata")
					spinner.SetTotal(1, true)
					return
				}

				// Analyze — spinner animates, text shows "Analyze"
				var ar *analyze.AnalyzeResult
				if needAnalyze {
					state.set("analyze")
					ar = analyze.Run(cr)
				}

				// Enrich — spinner animates, text shows "Enrich"
				var er *enrich.EnrichResult
				if needEnrich && ar != nil {
					state.set("enrich")
					er = enrich.RunForRepo(cfg, cr, ar, repo)
				}

				// Done — spinner clears, text shows "✓"
				state.set("done")
				spinner.SetTotal(1, true)

				resultCh <- render.RepoResult{
					Repo:    repo,
					Branch:  branch,
					Collect: cr,
					Analyze: ar,
					Enrich:  er,
				}
			}(repo, branch, spinner, state)
		}
	}

	go func() {
		wg.Wait()
		close(resultCh)
	}()

	var allResults []render.RepoResult
	for r := range resultCh {
		allResults = append(allResults, r)
	}

	p.Wait()

	// Restore stdout and log output
	os.Stdout = origStdout
	devNull.Close()
	log.SetOutput(os.Stderr)

	if logBuf.Len() > 0 {
		fmt.Fprint(os.Stderr, logBuf.String())
	}

	// ========== Render phase ==========
	if needRender && len(allResults) > 0 {
		fmt.Println("\n=== Render: Генерация отчётов ===")
		if err := render.RenderAll(allResults, cfg); err != nil {
			log.Fatalf("Render phase failed: %v", err)
		}
		fmt.Println("=== Render завершена ===")
	}

	fmt.Println("\n=== Готово ===")
}
