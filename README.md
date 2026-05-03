# SWE-bench Code Model Performance Monitor

## Purpose

This project provides an empirical framework for measuring the performance of code-focused language models like Claude Code, Codex, and Gemini on real-world software engineering tasks. It was built to provide objective, reproducible metrics that allow users to assess these tools for themselves, rather than relying on anecdotal reports or marketing claims.

The SWE-bench benchmark presents the model with actual GitHub issues from popular open-source projects and measures its ability to generate patches that successfully resolve these issues. This provides a concrete, measurable answer to the question: "How well do these code models actually perform on real software engineering tasks?"

> **Platform support:** The tools in this repository run on Linux, macOS, and Windows (including WSL). Replace `python` with `python3` on Unix-like systems or `py` on Windows if needed.

## Getting Started in 5 Minutes

```bash
# Assuming you have Python, a code model CLI (Claude or Codex), and Podman installed:
# Replace `python` with `python3` on Linux/macOS or `py` on Windows if needed.
git clone https://github.com/jimmc414/claudecode_n_codex_swebench.git
cd claudecode_n_codex_swebench
python -m pip install -r requirements.txt
python swe_bench.py run --limit 1  # Run your first test (~10 min)
python swe_bench.py check           # See your results
```

For detailed setup instructions, see [Prerequisites](#prerequisites) and [Installation](#installation) below.

## Quick Start (After Installation)

```bash
# 1. Run your first test (1 instance, ~10 minutes)
python swe_bench.py run --limit 1               # Claude Code (default)
python swe_bench.py run --limit 1 --backend codex  # Codex
python swe_bench.py run --limit 1 --backend gemini # Gemini

# 2. Check your results
python swe_bench.py check

# 3. Try a larger test when ready (10 instances, ~2 hours)
python swe_bench.py quick
```


## Prerequisites

Before starting, ensure you have:

1. **Python 3.8 or newer**
   ```bash
   python --version  # or python3/py --version
   ```

2. **Claude Code, Codex, or Gemini CLI installed and logged in**
   ```bash
   # Claude Code
   claude --version  # Should work without errors
   # Codex
   codex --version   # Should work without errors
   # Gemini
   gemini --version  # Should work without errors
   # If not logged in, run the relevant CLI
   ```

3. **Podman installed and running**
   ```bash
   podman --version           # Should show version
   podman machine list        # On macOS/Windows: a machine should be "Currently running"
   podman info >/dev/null     # Should succeed without errors
   ```
   - Needs ~50GB free disk space for images
   - 16GB+ RAM recommended
   - On macOS/Windows: bump the Podman machine to 8GB+ memory (`podman machine set --memory 8192 --cpus 4`)

   The benchmark talks to Podman via its Docker-compatible API socket. The runner
   resolves that socket automatically and exports `DOCKER_HOST` for the SWE-bench
   harness — no manual env setup required. See [Podman Setup](#podman-setup) below
   if you don't have Podman installed yet.

## Installation

```bash
# 1. Clone this repository
git clone <repository-url>
cd claudecode_n_codex_swebench

# 2. Install all Python dependencies (includes swebench)
python -m pip install -r requirements.txt  # Use python3/py as needed

# 3. Verify everything is working
python swe_bench.py list-models               # Claude models
python swe_bench.py list-models --backend codex  # Codex models
python swe_bench.py list-models --backend gemini # Gemini models

# Optional: Quick test to verify full setup
python swe_bench.py run --limit 1 --no-eval  # Test without Podman (2-5 min)
python swe_bench.py run --limit 1            # Full test with Podman (10-15 min)
```

### Troubleshooting Setup

If you get errors:

- **"Claude CLI not found"**: Install from https://claude.ai/download
- **"Codex CLI not found"**: Ensure `codex` is installed and in your PATH
- **"Gemini CLI not found"**: Ensure `gemini` is installed and in your PATH
- **"Podman not ready" / no socket**: macOS/Windows — `podman machine start`. Linux — `systemctl --user start podman.socket`.
- **"swebench not found"**: Run `pip install swebench`
- **Out of memory**: On macOS/Windows, raise the Podman machine memory: `podman machine stop && podman machine set --memory 8192 --cpus 4 && podman machine start`

## Command Reference

### Main Tool: `swe_bench.py`

The unified tool provides all functionality through a single entry point:

```bash
# Default: Run full 300-instance benchmark
python swe_bench.py

# Quick commands
python swe_bench.py quick          # 10 instances with evaluation
python swe_bench.py full           # 300 instances with evaluation
python swe_bench.py check          # View scores and statistics
python swe_bench.py list-models    # Show available models (Claude by default)
python swe_bench.py list-models --backend codex  # Show Codex models
python swe_bench.py list-models --backend gemini # Show Gemini models
```

### Running Benchmarks

```bash
# Basic runs with different sizes
python swe_bench.py run --quick                    # 10 instances
python swe_bench.py run --standard                 # 50 instances
python swe_bench.py run --full                     # 300 instances
python swe_bench.py run --limit 25                 # Custom count

# Model selection (September 2025 models)
python swe_bench.py run --model opus-4.1 --quick   # Latest Opus
python swe_bench.py run --model sonnet-3.7 --limit 20
python swe_bench.py run --model best --quick       # Best performance alias

# Performance options
python swe_bench.py run --quick --no-eval          # Skip Podman evaluation
python swe_bench.py run --limit 20 --max-workers 4 # More parallel containers

# Dataset selection
python swe_bench.py run --dataset princeton-nlp/SWE-bench_Lite --limit 10
```

### Running Specific Test Instances

When establishing a baseline or debugging specific issues, you can run SWE-bench against individual test instances:

```bash
# Using code_swe_agent.py directly (patch generation only)
python code_swe_agent.py --instance_id django__django-11133

# Specify backend explicitly
python code_swe_agent.py --instance_id django__django-11133 --backend codex

# With full SWE-bench dataset instead of Lite
python code_swe_agent.py --instance_id django__django-11133 --dataset_name princeton-nlp/SWE-bench

# With specific model for baseline comparison
python code_swe_agent.py --instance_id django__django-11133 --model opus-4.1

# Finding available instance IDs
python -c "from datasets import load_dataset; ds = load_dataset('princeton-nlp/SWE-bench_Lite', split='test'); print('\\n'.join([d['instance_id'] for d in ds][:20]))"
```

**Use Cases for Single Instance Testing:**
- Establishing performance baselines for specific problem types
- Debugging Claude Code's approach to particular challenges
- Comparing model performance on identical problems
- Validating fixes after prompt or model updates

**Note:** Instance IDs follow the format `<repo>__<repo>-<issue_number>` (e.g., `django__django-11133`, `sympy__sympy-20154`)

### Evaluating Past Runs

```bash
# Interactive selection menu
python swe_bench.py eval --interactive

# Specific file
python swe_bench.py eval --file predictions_20250902_163415.jsonl

# By date
python swe_bench.py eval --date 2025-09-02
python swe_bench.py eval --date-range 2025-09-01 2025-09-03

# Recent runs
python swe_bench.py eval --last 5

# Preview without running
python swe_bench.py eval --last 3 --dry-run
```

### Viewing Scores

```bash
# Basic score view
python swe_bench.py scores

# With statistics and analysis
python swe_bench.py scores --stats --trends

# Filter results
python swe_bench.py scores --filter evaluated      # Only evaluated runs
python swe_bench.py scores --filter pending        # Only pending evaluation

# Export to CSV
python swe_bench.py scores --export results.csv

# Recent entries
python swe_bench.py scores --last 10
```

## Model Selection

### Available Models (September 2025)

```bash
# View all available models and their expected performance
python swe_bench.py list-models
```

**Opus Models** (Most Capable):
- `opus-4.1`: Latest, 30-40% expected score
- `opus-4.0`: Previous version, 25-35% expected score

**Sonnet Models** (Balanced):
- `sonnet-4`: New generation, 20-30% expected score
- `sonnet-3.7`: Latest 3.x, 18-25% expected score
- `sonnet-3.6`: Solid performance, 15-22% expected score
- `sonnet-3.5`: Fast/efficient, 12-20% expected score

**Aliases**:
- `best`: Maps to opus-4.1
- `balanced`: Maps to sonnet-3.7
- `fast`: Maps to sonnet-3.5

You can also use any model name accepted by Claude's `/model` command, including experimental or future models not yet in the registry.

## Understanding Scores

### Two Types of Scores

1. **Generation Score**: Percentage of instances where a patch was created (misleading)
2. **Evaluation Score**: Percentage of instances where the patch actually fixes the issue (real score)

Only the evaluation score matters. A 100% generation score with 20% evaluation score means Claude Code created patches for all issues but only 20% actually worked.

### Expected Performance Ranges

Based on empirical testing with SWE-bench:

| Score Range | Performance Level | What It Means |
|------------|------------------|---------------|
| 0-5% | Poor | Patches rarely work, significant issues |
| 5-10% | Below Average | Some success but needs improvement |
| 10-15% | Average | Decent performance for an AI system |
| 15-20% | Good | Solid performance, useful for real work |
| 20-25% | Very Good | Strong performance, competitive |
| 25-30% | Excellent | Top-tier performance |
| 30%+ | Outstanding | Exceptional, rare to achieve |

### Time Estimates

| Test Size | Instances | Generation | Evaluation | Total Time |
|-----------|-----------|------------|------------|------------|
| Quick | 10 | ~20-30 min | ~30-50 min | ~1-2 hours |
| Standard | 50 | ~2-3 hours | ~3-5 hours | ~5-8 hours |
| Full | 300 | ~12-15 hours | ~20-30 hours | ~35-45 hours |

## Project Structure

```
claudecode_n_codex_swebench/
├── swe_bench.py              # Main unified tool (all commands)
├── code_swe_agent.py         # Core agent for Claude Code or Codex
├── USAGE.md                  # Detailed command usage guide
├── benchmark_scores.log      # Results log (JSON lines format)
├── requirements.txt          # Python dependencies
│
├── utils/                    # Core utilities
│   ├── claude_interface.py  # Claude Code CLI interface
│   ├── prompt_formatter.py  # Formats issues into prompts
│   ├── patch_extractor.py   # Extracts patches from responses
│   └── model_registry.py    # Model definitions and aliases
│
├── prompts/                  # Prompt templates
│   ├── swe_bench_prompt.txt # Default prompt
│   ├── chain_of_thought_prompt.txt
│   └── react_style_prompt.txt
│
├── predictions/              # Generated predictions (JSONL)
├── results/                  # Detailed Claude outputs
├── evaluation_results/       # Podman evaluation results
└── backup/                   # Archived/unused files
```

## Verification Checklist

Use this checklist to verify Claude Code is working properly:

```bash
# 1. Test setup (should list models)
python swe_bench.py list-models

# 2. Single instance test (5-10 minutes)
python swe_bench.py run --limit 1 --no-eval

# 3. Quick test with evaluation (1-2 hours)
python swe_bench.py quick

# 4. Check scores (should show real evaluation scores)
python swe_bench.py check

# 5. If scores look good, try larger test
python swe_bench.py run --standard  # 50 instances
```

## Troubleshooting

### Common Issues

**Claude CLI not found**
```bash
# Ensure claude is in PATH
which claude
# If not found, reinstall Claude Code or add to PATH
```

**Podman socket not reachable**
```bash
# macOS / Windows
podman machine start

# Linux (rootless, recommended)
systemctl --user enable --now podman.socket
```

**Out of memory during evaluation**
```bash
# Reduce parallel workers
python swe_bench.py run --quick --max-workers 1
```

**Evaluation times out**
- Some instances take longer to test
- Default timeout is 30 minutes per instance
- This is normal for complex codebases

**Low scores (0-5%)**
- This may be normal for certain models or datasets
- Try with a better model: `--model opus-4.1`
- Check that Claude Code is properly authenticated

### Log Files

- **benchmark_scores.log**: Main results log (JSON lines)
- **predictions/**: All generated patches
- **evaluation_results/**: Detailed Podman test results
- **results/**: Raw Claude Code outputs for debugging

## Podman Setup

The benchmark runs the SWE-bench harness against Podman's Docker-compatible API.
The runner discovers the local Podman socket and exports `DOCKER_HOST` to the
harness automatically — you only need a running Podman.

### macOS
```bash
brew install podman

# Provision and start the Podman VM (8GB RAM / 4 CPUs is a good baseline)
podman machine init --memory 8192 --cpus 4 --disk-size 100
podman machine start

# Sanity check
podman info >/dev/null && echo "Podman is ready"
```

### Ubuntu/Debian Linux (rootless, recommended)
```bash
sudo apt update
sudo apt install -y podman

# Enable the user-level API socket that docker-py will talk to
systemctl --user enable --now podman.socket

# Sanity check
podman info >/dev/null && echo "Podman is ready"
```

### Fedora / RHEL
```bash
sudo dnf install -y podman
systemctl --user enable --now podman.socket
```

### Windows
```powershell
# Install via winget (or download from https://podman.io/)
winget install --id RedHat.Podman

podman machine init --memory 8192 --cpus 4 --disk-size 100
podman machine start
```

### Verify Podman is Ready
```bash
# Version and daemon status
podman --version
podman info >/dev/null

# (macOS/Windows only) machine should be running
podman machine list

# Smoke test
podman run --rm hello-world
```

### Apple Silicon note
SWE-bench images are `linux/amd64`. Podman emulates via QEMU on Apple Silicon,
which works but is slower than native runs. Expect longer evaluation times.


## License

This benchmarking framework is provided as-is for empirical evaluation purposes. SWE-bench is created by Princeton NLP. Claude Code is a product of Anthropic.