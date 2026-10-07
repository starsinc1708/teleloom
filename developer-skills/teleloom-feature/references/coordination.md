# Authorized subagent work

Use this reference when the user/task explicitly requests subagents. Agent
delegation does not require separate user-owned Codex threads.

1. The parent fixes the accepted feature contracts and dependency graph. Read-only
   investigation can run in parallel; implementation waits for required contracts.
2. Assign a vertical behavior slice with exclusive file ownership. Shared files
   such as `server.py`, `runtime.py`, `adapters.py`, `models.py`, `jobs.py` and
   `store.py` have one writer at a time in a shared checkout. An agent needing an
   unowned/shared edit returns a proposed patch to the parent or waits for an
   explicit ownership transfer. Never overwrite another agent's changes.
3. Use separate feature modules/test files where the existing design supports
   them. The parent integrates the narrow shared hooks. Do not invent a generic
   plugin framework just to allow parallel editing.
4. Each assignment states the accepted contract, prerequisites, allowed files,
   tests, completion evidence and forbidden side effects. Agents report changed
   files, commands/results, limitations and integration needs; they do not commit
   another agent's work or alter live profile permissions.
5. Use as many workers as the environment actually supports, reserving the parent
   for integration. Prefer two or three substantial slices to many tiny tasks.
6. Integrate dependency-first and check affected public workflows once combined.
   Before a substantial release, run the full suite once unless the owner cancels
   it. Do not repeat every agent's completed checks. Independent reviews apply
   when the accepted task or owner requests them.

For the six reading directions, start with canonical evidence/compatibility and
evaluated folder contracts. Folder membership unlocks activity comparison and
multi-chat evidence. Thread/topic retrieval and selected attachment extraction
can then proceed alongside those jobs once shared message/capability hooks are
owned and stable. This is a dependency guide, not permission to expand the task.

Task packet:

```text
Implement: <one observable behavior and accepted contract>
Prerequisites: <stable interfaces or completed issue IDs>
Own: <exclusive source/test/doc paths>
Shared hooks: <parent-owned files and requested changes>
Prove: <public MCP/CLI/workflow acceptance tests>
Constraints: fake external APIs/clocks; real temp SQLite/queues; no live
Telegram mutations, credential access or permission/config changes.
Return: patch/file list, test evidence, uncovered cases and integration needs.
```
