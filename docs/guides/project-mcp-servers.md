# Letting a project run its own MCP servers and hooks

A project can declare MCP servers in its own agent spec, under
`<project>/.kiro/agents/*.json` → `mcpServers`. On a Claude, Codex, Goose or
OpenCode session, each of those servers is a command that starts **as you, outside
the tool sandbox**, as soon as the session opens. So a project you just cloned
does not get to start them. Its spec `hooks` run as you too, so they are part of the
same choice. Both stay off until you say yes for that folder.

kiro-cli sessions are not affected: kiro-cli reads the project spec itself.

## Turn them on for one project

> **Trust only a project whose code you'd run yourself. An agent working here can
> change that code.** A grant notices when the agent spec changes, but not when a
> script it runs (`./scripts/server.sh`, `tools/mcp.js`) is rewritten.

1. Open a chat whose project is that folder.
2. If the project declares servers, a notice appears above the message box:
   "This project declares N MCP servers. They stay off until you trust them."
3. Click **Review**. The dialog shows the folder's real path, each server's command and
   arguments, and each hook's event and command. Environment variables and headers are
   shown by name only.
4. Click **Trust and run** only if you know the project. Click **Keep them off** to
   leave things as they are.

The servers start from the **next** session in that folder.

## Turn them off again

Open **MCP** settings. The **Projects trusted to run MCP servers** list shows every
folder you trusted, including ones that no longer exist. Click **Withdraw** next to
a folder. New sessions there start without its servers.

## What stays the same

- A project's switch-offs always apply, trusted or not: `disabledTools` on a server,
  and `"disabled": true`.
- Trusting a project's **skills** is a different choice. It does not let the
  project's MCP servers run, and trusting MCP servers does not load its skills.
- Your own servers in `~/.kiro/agents` and `~/.kiro/settings/mcp.json` are not
  affected.

## When a grant stops matching

A grant is tied to the exact folder you reviewed. It no longer counts, and the
servers stay off, when:

- the project changes a server's command, arguments, URL, environment, headers or any
  other launch setting, or adds or edits a hook (turning tools off, muting a server or
  turning a hook off or on does not count);
- the folder is deleted and a new one is made at the same path;
- the project folder you open is a symlink;
- the grant store is unreadable or was edited by hand into a shape it does not know.

Grant it again from the notice if you still trust the project.

Only the dashboard owner can view, grant or withdraw these grants.
