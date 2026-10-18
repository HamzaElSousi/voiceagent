# Security Policy

VoiceAgent stores provider API keys and handles phone numbers and call transcripts, so security
reports are taken seriously.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub:
**Security > Report a vulnerability** on this repository (GitHub private vulnerability reporting).
Do not open a public issue.

Include what you found, how to reproduce it, and the impact you expect. You should get an
acknowledgement within 5 business days. Once a fix is released, you will be credited in the release
notes unless you prefer otherwise.

## Supported versions

Security fixes land on the latest release only. Self-hosters should keep their instance up to date
(`git pull && docker compose up -d --build`, or pull the latest image).

## Scope

In scope: authentication and session handling, tenant isolation between workspaces, encryption of
stored provider keys, Twilio and webhook signature validation, and leakage of keys, phone numbers or
transcripts through logs or the API.

Out of scope: vulnerabilities in third-party providers (Twilio, Deepgram, ElevenLabs, Anthropic,
Cal.com), and issues that need an already-compromised server or instance admin account.
