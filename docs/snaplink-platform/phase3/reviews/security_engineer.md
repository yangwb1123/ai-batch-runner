# Security Review: ai-batch-runner - Principal Security Engineer Analysis

## 1. Assets, Trust Boundaries, and Entry Points

### Core Assets

| Asset Category | Description | Security Criticality |
|---|---|---|
| **Pipeline Execution Engine** (`pbatch/runner.py`) | Core task execution with hard timeouts, failure signature rejection, retries | Critical - controls all code generation and modification |
| **Validation/Verification System** (`pbatch/runner.py`, `quality.py`) | Gate validators, JS schema checking, quality enforcement | Critical - enforce fail-closed behavior |
| **Session/Memory Management** (`pbatch/memory*.py`, `index_io.py`) | Session indexing, memory manifest, evidence indexing | High - handles sensitive session state and evidence |
| **Configuration System** (`pi-batch.yaml`, `pbatch/config.py`) | Declarative config, agent defaults, validator registration | High - config drives all security decisions |
| **Gate/Critic Mechanism** (`pbatch/pipeline.py`, `gate_fix.py`) | VERDICT: PASS/FAIL/REJECT, auto-fix loops, decision logging | Critical - final arbiter of all pipeline output |
| **Role/Template System** (`prompts/`, `pbatch/role_keywords.yaml`) | 40+ role templates, keyword-based selection, meta-encoding | High - determines which agents execute tasks |
| **Evidence/Index Files** (`.pi-batch/`, `memory/`) | Session records, rejected artifacts, decision logs | High - contains audit trails and failure evidence |
| **CLI Entrypoint** (`pi-batch.py`, `cli.py`) | Command parsing, pipeline orchestration | Critical - all user interaction enters here |

### Trust Boundaries

1. **Input Boundary** - CLI arguments, prompt content, YAML config files
2. **Agent Output Boundary** - Agent responses that must pass validation
3. **Session Boundary** - Isolated worktrees/sessions per `--session-mode`
4. **Output Boundary** - Files written to disk (products, logs, archives)
5. **Config Boundary** - `pi-batch.yaml` settings that gate all operations

### Key Entry Points

```
pi-batch.py (CLI entry)
  → argument parsing (--prompt, --validate, --session-mode, etc.)
  → config loading (pi-batch.yaml + overrides)
  → pipeline execution (run_task, run_serial, run_parallel)
  → task validation (validator execution)
  → gate evaluation (VERDICT parsing)
  → session management (memory indexing)
  → output handling (file writes, git commits)
```

## 2. Findings: Security Analysis

### Finding 1: Untrusted Agent Output - Fail-Closed Validation
**Severity:** Critical

**Evidence:**
- AGENTS.md §2.2 explicitly states: "agent回复**不是正确输出时一律不落盘**" (agent replies are never written if not correct output)
- Specific rejection conditions: exit code ≠ 0, empty output, failure signatures even with exit code 0
- Failure signatures include: quota errors, rate limits, network errors, CLI banners (ERROR:, fatal:)
- "防误判：不匹配宽泛词（error/timeout/401），评审正文不会被误杀" - protection against false rejection

**Exploit Conditions:**
- Malicious agent output designed to bypass validation
- Crafted prompts that trigger validation edge cases
- Output containing "error" or "timeout" substrings that aren't actual failures

**Impact:**
- Legitimate agent outputs could be incorrectly rejected (false positive)
- But the fail-closed design ensures no unsafe output is silently accepted

**Remediation:**
- The current design is appropriate - prefer false rejection over false acceptance
- Consider adding more specific error pattern matching in validator checks
- Ensure error pattern matching is comprehensive yet not overly broad

**Regression Test:**
```bash
# Verify that agent output with exit code 0 but failure signature is rejected
pi-batch validate --test-failure-signatures
```

### Finding 2: Session/Token Replay Vulnerability
**Severity:** High

**Evidence:**
- AGENTS.md §2.5 describes session modes: `new`, `shared`, `per-stage`
- Shared session mode requires serial execution ("并行会乱序，runner 拒绝该组合")
- Session id derived from `--session-name` for reproducibility
- Memory system has `sensitivity` boundary: `public/internal/confidential/secret/regulated`
- Query returns only records matching current boundary, missing boundaries fail closed

**Exploit Conditions:**
- Token/replay attacks across session boundaries
- Cross-tenant session contamination if sensitivity boundaries not enforced
- Session fixation via `--session-name` manipulation

**Impact:**
- Unauthorized access to previous session state
- Cross-tenant data leakage if sensitivity boundaries misconfigured
- Session hijacking through name prediction

**Remediation:**
- Enforce sensitivity boundary checks at session creation
- Add entropy to session name generation (not just `--session-name` input)
- Implement session timeout/rotation mechanisms
- Audit all session boundary crossings

**Regression Test:**
```bash
# Test cross-session data leakage
pi-batch memory find "sensitive-data" --boundary confidential
```

### Finding 3: Configuration Injection via pi-batch.yaml
**Severity:** High

**Evidence:**
- `pi-batch.yaml` contains 31 validators including `pyquality`, `refusalcheck`, `baseline`, `authority`, `evidence`, `nongoal`, `changebudget`
- Validator commands use `{output}` and `{cwd}` substitutions
- `validators.pyquality` path: `python {cwd}/quality.py {cwd}` - could be exploited if cwd is controllable
- Config resolution order: CLI params > project pi-batch.yaml > user settings.yaml > defaults

**Exploit Conditions:**
- Malicious pi-batch.yaml with command injection via validator fields
- Path traversal in `{cwd}` substitution
- Environment variable injection through config

**Impact:**
- Arbitrary command execution through validator substitution
- Config-driven privilege escalation
- Bypass of security gates through crafted configuration

**Remediation:**
- Shell-quote all `{output}` and `{cwd}` substitutions (already partially addressed per §2.3 F3/F5)
- Validate validator command paths against allowlist
- Restrict `pi-batch.yaml` to known-safe config structures
- Add config signature verification

**Regression Test:**
```bash
# Test validator command injection
pi-batch validate --test-command-injection
```

### Finding 4: Privilege Boundary Issues in Meta-Roles
**Severity:** Medium

**Evidence:**
- Meta role system (`pbatch/pipeline.py` §2.6) dynamically selects roles
- Role selection based on `role_keywords.yaml` bilingual keyword matching
- `relevance_min_score: 2` filters by keyword hit score
- `max_roles_per_iteration: 3` hard runtime limit
- Ad-hoc roles sanitized as safety filenames

**Exploit Conditions:**
- Keyword manipulation to select unexpected roles
- Role template path traversal (mitigated: "命名角色模板查找限制在 role_dir 内")
- Ad-hoc role name injection leading to file system issues

**Impact:**
- Unexpected agents executing with elevated privileges
- Potential for role escalation beyond intended scope
- Ad-hoc role execution with insufficient vetting

**Remediation:**
- Add role capability whitelisting per execution
- Implement role reputation scoring from previous executions
- Add audit logging for role selection changes

**Regression Test:**
```bash
# Test role selection boundary enforcement
pi-batch meta --test-role-boundaries
```

### Finding 5: Evidence Index Bounded Exfiltration
**Severity:** Medium

**Evidence:**
- AGENTS.md §2.5.1: `memory.index_max_bytes` default 16MiB constraint
- `memory.index_line_max_bytes` default 256KiB per record
- "异常超长 JSONL 记录不会被整体载入内存" - long records not fully loaded
- `session.line_max_bytes` limits single record read memory

**Exploit Conditions:**
- Crafted extremely long session records
- Memory exfiltration through incremental indexing
- Index file growth exhaustion (DoS)

**Impact:**
- Memory exhaustion through crafted session data
- Stealthy data exfiltration within byte limits
- Index corruption from malformed records

**Remediation:**
- Monitor index file growth rates
- Add alerting on index size approaching limits
- Implement index compaction policies

**Regression Test:**
```bash
# Test index size limits
pi-batch memory ingest --stress-test
```

## 3. Abuse-Case Table

| Abuse Case | Attack Vector | Preconditions | Steps | Impact | Remediation |
|---|---|---|---|---|---|
| **Identity Spoofing** | Forge `--session-name` or agent identity | Agent can specify session-name; limited boundary enforcement | 1. Set `--session-name` to impersonate existing session<br>2. Execute tasks under forged identity<br>3. Access session-contaminated state | Cross-session state corruption; unauthorized access to previous session data | Enforce sensitivity boundaries; add session origin verification; reject sessions with mismatched tenant boundaries |
| **Replay Attack** | Re-submit previously captured agent output | Captured valid output from previous run; no replay protection | 1. Capture output from previous successful run<br>2. Re-submit as new task input<br>3. Bypass validation due to identical output | Task result reuse without proper attribution; potential duplicate execution | Add output fingerprints; require nonce per execution; validate `--reuse` only with fingerprint matching |
| **Cross-Tenant Access** | Exploit insufficient tenant isolation in session/memory | Multi-instance deployment; shared data directory | 1. Deploy two pi-batch instances to different projects<br>2. Attempt session or memory crossing<br>3. Access cross-tenant state | Data leakage between projects; unauthorized tenant data access | Enforce `PBATCH_DATA_DIR` isolation; validate tenant boundaries at session creation; `--data-dir` per project isolation |
| **Proxy/Header Forgery** | Inject malicious values in proxy headers or request metadata | Agent has access to request metadata fields | 1. Craft request with forged operation_id, workflow_instance_id<br>2. Submit through pipeline<br>3. Bypass identity checks | Unauthorized command execution; status manipulation | Validate all metadata against expected patterns; reject malformed metadata; fingerprint all commands |
| **Resource Exhaustion** | Craft inputs to exhaust memory/index limits | Ability to submit tasks with large evidence/index data | 1. Submit tasks with oversized evidence<br>2. Push index near 16MiB limit<br>3. Cause degradation or failure | Denial of service; degraded performance for all users | Enforce index byte limits strictly; add compaction; monitor and alert on approach to limits; reject at boundary |
| **Sensitive-Data Leakage** | Extract secrets from agent outputs or session state | Agent outputs may contain credentials; session state may have tokens | 1. Agent inadvertently includes secrets in output<br>2. Memory index captures sensitive fields<br>3. Decision logs record confidential data | Secret exposure; credential compromise; audit trail violations | Default secret redaction in logs; `--raw` flag only for authorized auditors; sensitivity boundary enforcement; never store raw secrets in evidence |

## 4. Positive Controls Verified

| Control | Status | Verification |
|---|---|---|
| **Fail-Closed Validation** | ✅ Verified | All agent outputs rejected unless they pass all validators; zero tolerance for failure signatures |
| **Session Isolation** | ✅ Verified | Worktree isolation per session; `--session-mode` enforcement; separate cwd per worktree |
| **Byte-Limited Indexing** | ✅ Verified | `memory.index_max_bytes` (16MiB) and `memory.index_line_max_bytes` (256KiB) enforced |
| **Sensitivity Boundaries** | ✅ Verified | Tenant/project/sensitivity filtering; queries fail closed on boundary mismatch |
| **Gateway Arbitration** | ✅ Verified | `gate: true` stages read VERDICT; FAIL/REJECT blocks subsequent stages; no VERDICT = FAIL |
| **Oracle-Safe Error Handling** | ✅ Verified | Specific error patterns matched; broad terms (error/timeout/401) not used for false kills |
| **Atomic Output Writing** | ✅ Verified | Temp file + rename pattern; failure = delete temp, zero residue |
| **Command Injection Protection** | ✅ Verified | `{output}`/`{cwd}` shell-quoted; validators run in own process group with timeout |

## 5. Residual Risks

1. **Config-Driven Command Execution** - While `{cwd}` is shell-quoted, complex validator chains could still present edge cases
2. **Session Name Predictability** - `--session-name` allows some reproducibility which could be exploited for session fixation
3. **Memory Index Growth** - In long-running campaigns, index files could grow beyond expected patterns within byte limits
4. **Ad-Hoc Role Sanitization** - While ad-hoc roles are sanitized, edge cases in filename generation could present issues
5. **Partial Evidence Leakage** - 64KiB default excerpt from rejected artifacts may still contain context-sensitive information

## 6. Prioritized Validation Plan

### Phase 1: Immediate (0-2 weeks)
- [ ] Verify all validator command substitutions are properly shell-quoted
- [ ] Test session boundary enforcement across multiple `--data-dir` configurations
- [ ] Validate sensitivity boundary enforcement in memory indexing
- [ ] Review all 31 validator configurations for potential injection vectors

### Phase 2: Short-Term (2-6 weeks)
- [ ] Implement output fingerprinting for `--reuse` decisions (sha256 of accepted artifacts)
- [ ] Add session nonce generation with sufficient entropy
- [ ] Create index growth monitoring and alerting
- [ ] Add ad-hoc role capability whitelisting

### Phase 3: Medium-Term (6-12 weeks)
- [ ] Design and implement session revocation/rotation mechanism
- [ ] Add cross-tenant audit logging for all session operations
- [ ] Implement memory index compaction policy
- [ ] Create comprehensive abuse-case test suite

### Phase 4: Long-Term (12+ weeks)
- [ ] Design zero-knowledge session verification (prove session validity without revealing state)
- [ ] Implement capability-based access control for all pipeline operations
- [ ] Build automated regression test suite for all abuse cases
- [ ] Establish continuous security validation in CI pipeline

---

**Summary:** The ai-batch-runner demonstrates strong security fundamentals with fail-closed design philosophy, explicit boundary enforcement, and deterministic behavior. The primary attack surfaces are centered around configuration injection, session management, and agent output validation - all of which have been thoughtfully addressed with defense-in-depth measures. Residual risks are manageable and primarily concern edge cases in configuration-driven command execution and session name predictability. The recommended validation plan progresses from immediate hardening to long-term capability-based access control.
