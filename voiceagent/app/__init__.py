"""The VoiceAgent platform: accounts, workspaces, encrypted provider keys, agents and call history,
served as a JSON API under /api. The call engine itself (voiceagent.session and friends) stays
independent of this package, so the CLI dev mode keeps working with no database."""
