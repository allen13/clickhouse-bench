# Makefile — clickhouse-bench developer targets.
#
# Most of the work runs through `uv run clickhouse-bench …` (Click CLI in
# src/main.py). This file wraps the recurring flows so you don't have to
# remember the command sequences.
#
# Quick reference:
#   make paper             rebuild paper.pdf from paper.tex
#   make paper-clean       remove LaTeX intermediates
#   make aggregate         regenerate results/aggregate.md from JSONs
#   make smoke             quick connection test against ClickHouse Cloud
#   make help              this listing

PAPER     := paper
LATEX     := xelatex
LATEX_OPTS := -interaction=nonstopmode -halt-on-error

.PHONY: help
help:
	@echo "clickhouse-bench targets:"
	@echo ""
	@echo "  -- Documentation --"
	@echo "  paper           rebuild paper.pdf from paper.tex (xelatex, two passes)"
	@echo "  paper-clean     remove LaTeX intermediates (.aux, .log, .out, …)"
	@echo "  aggregate       regenerate results/aggregate.md from results/*.json"
	@echo ""
	@echo "  -- Benchmark pipeline --"
	@echo "  smoke           connect to ClickHouse Cloud and run a 1-row query"
	@echo "  setup           uv run clickhouse-bench setup --drop"
	@echo "  seed-small      seed at scale=100000 (1.9M rows)"
	@echo "  seed-large      seed at scale=1000000 (19M rows)"
	@echo "  benchmark       uv run clickhouse-bench benchmark --warm-runs 5"
	@echo "  evaluate        uv run clickhouse-bench evaluate"
	@echo "  features-all    run every compare-features key sequentially"
	@echo "  experiments-all run every scripts/experiments/{c,d,e}*.py"
	@echo "  cleanup         drop all cmp_* and exp_* tables"

# ----------------------------------------------------------------------------
# Paper
# ----------------------------------------------------------------------------

.PHONY: paper
paper: $(PAPER).pdf

$(PAPER).pdf: $(PAPER).tex
	@command -v $(LATEX) >/dev/null || \
	  (echo "ERROR: $(LATEX) not found. Install via 'brew install --cask mactex-no-gui' or 'apt install texlive-xetex'." && exit 1)
	$(LATEX) $(LATEX_OPTS) $(PAPER).tex
	$(LATEX) $(LATEX_OPTS) $(PAPER).tex   # second pass for cross-references
	@echo ""
	@echo "→ $(PAPER).pdf $$(du -h $(PAPER).pdf | cut -f1)"

.PHONY: paper-clean
paper-clean:
	@rm -f $(PAPER).aux $(PAPER).log $(PAPER).out $(PAPER).toc \
	       $(PAPER).synctex.gz $(PAPER).fls $(PAPER).fdb_latexmk \
	       $(PAPER).bbl $(PAPER).blg
	@echo "removed LaTeX intermediates (paper.pdf preserved)"

.PHONY: aggregate
aggregate:
	uv run python scripts/aggregate_for_paper.py

# ----------------------------------------------------------------------------
# Pipeline
# ----------------------------------------------------------------------------

.PHONY: smoke
smoke:
	uv run python -c "from src.config import get_client; \
	c = get_client(); print('OK', c.query('SELECT version(), currentUser()').result_rows[0])"

.PHONY: setup
setup:
	uv run clickhouse-bench setup --drop

.PHONY: seed-small
seed-small:
	uv run clickhouse-bench seed --scale 100000

.PHONY: seed-large
seed-large:
	uv run clickhouse-bench seed --scale 1000000

.PHONY: benchmark
benchmark:
	uv run clickhouse-bench benchmark --warm-runs 5

.PHONY: evaluate
evaluate:
	uv run clickhouse-bench evaluate

.PHONY: features-all
features-all:
	@for k in ordering lowcardinality codecs projections \
	          materialized_views skip_indexes partitioning engines \
	          index_granularity bloom_filter_fpr skip_index_granularity; do \
	  echo "== $$k =="; \
	  uv run clickhouse-bench compare-features --comparison "$$k" || exit 1; \
	done

.PHONY: experiments-all
experiments-all:
	@for s in scripts/experiments/c*.py scripts/experiments/d*.py \
	          scripts/experiments/e*.py; do \
	  echo "== $$s =="; \
	  uv run python "$$s" || exit 1; \
	done

.PHONY: cleanup
cleanup:
	uv run clickhouse-bench cleanup-variants
