# INTERIM (prod agent): VOTEBOT-83 deploy request (`4ff63bf`, amended `92b313c`) read; nothing run. 2026-11-27 (production host)

Read in full. Not a go, so nothing runs. It needs Ramon's go in my session naming `71f177175435a94b2fa03ff81b7df003ca1cf6f8`. Production VoteBot is at `4a85cf0` (the 11-27 deploy). When I run it I will do the preflight exactly as written (the diff against `4a85cf0` must list only the five files; I stop if not), check no chat in the last 10 minutes, recreate only the 3.0 container (the 2.0 server is not touched), and report checks A to E with real output.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
