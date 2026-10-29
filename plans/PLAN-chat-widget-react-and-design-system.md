# PLAN: the chat widget on React and the DDP design system (VOTEBOT-62)

Status: **recommendation, for Ramon to agree or change.** Nothing is built. VOTEBOT-62 asks for this first ("decide how the widget is delivered, in a short written recommendation") and says no building starts before it is agreed. Written 2026-10-28.

## Recommendation in five lines

1. **Build the chat once, as a React component that lives in ddp-next** (`src/components/chat/`). No separate Next.js app, no script tag, no global, no Shadow DOM.
2. **The Voatz in-app page becomes a small route of ddp-next** that renders the same component full screen and reads `?ddp_url=`, so there is one chat codebase. The hostname and the `?ddp_url=` link form stay as they are (they are already in Voatz).
3. **Do not wait for the rebuild to move the Voatz page to VoteBot 3.0.** VOTEBOT-54 does that with today's bundle; the rebuild replaces it later.
4. **Use the design system for what it has** (tokens, `Button`, `IconButton`, `TextInput`) and build the chat-specific pieces (launcher, panel, message list) in ddp-next.
5. **Close the page-address leak first, as its own change** (VOTEBOT-63, PR #81), so the rebuild does not carry it.

## What I checked (2026-10-28)

| Fact | Where |
|---|---|
| ddp-next is Next 16 / React 19 and uses `@digital-democracy-project/design-system` **0.4.0 by version**, resolved from `registry.npmjs.org`. The link to a local checkout is opt-in per machine (`DDP_DESIGN_SYSTEM_PATH`, `dev/link-design-system.mjs`); production and CI get the published package. **The ticket said ddp-next consumes it "by link": that is only true on a developer machine.** | `ddp-next/package.json`, `package-lock.json`, `dev/link-design-system.mjs` |
| The package's license field is `UNLICENSED`. `npm view` returns it from this machine; I did not check whether this machine's npm is logged in, so **I cannot say whether it is readable without an account.** | `ddp-design-system/package.json` |
| The design system ships: `Badge, Button, Card, Checkbox, ClickableTile, DropdownMenu, Icon, IconButton, Menu, MenuItem, SearchBar, SectionLabel, Select, TextInput, ToggleButton, ToggleSwitch`, SCSS tokens and one `styles.css`. **It has no message bubble, panel/dialog, launcher, typing indicator or Markdown renderer.** | `ddp-design-system/components/` |
| ddp-next loads the widget with two `<Script>` tags (`VoteBotChat.tsx`): a config script that sets `window.DDPChatConfig`, then `/widget/ddp-chat.min.js`. Pages talk to it through `window.DDPChatWidget.setPageContext` (`src/lib/votebot-context.ts`). The whole integration is 232 lines in five files. | `ddp-next/src/components/chat/`, `src/lib/votebot-context.ts` |
| ddp-next's `public/widget/ddp-chat.min.js` is **byte-identical** to votebot `main`'s `chat-widget/dist/ddp-chat.min.js` (sha256 `73ec70a4...`, 120,164 bytes at `ae14552`). | both files |
| The widget is 2,257 lines of JS (`widget.js` 871, `ui.js` 869, `chat.js` 282, `websocket.js` 235) and 800 of CSS, no framework, no runtime dependency, rendered in a Shadow DOM. Tests: `page-context`, `quick-actions`, `markdown-links`, `webflow-embed` (Node runner, each run against `src` and the committed `dist`), plus `page-address` (VOTEBOT-63). | `chat-widget/` |
| The Voatz page (`chat-widget/index.html`, 3.4 KB) is a landing page with **its own colours** and a "Start a Conversation" button, loading `./dist/ddp-chat.min.js`. With `?ddp_url=` it auto-opens the chat. Its `wsUrl` is hard-coded to `wss://api.digitaldemocracyproject.org/ws/chat`, **the VoteBot 2.0 address**, so moving the page to 3.0 also means changing that line (VOTEBOT-54 step 3). | `chat-widget/index.html` |
| The visitor id lives in `localStorage` (`ddp_votebot_visitor_id`); session id, history and popup state in `sessionStorage` under the `ddp_votebot_` prefix, with a 30-minute timeout. | `chat-widget/src/websocket.js` |

Not checked: how the Voatz in-app browser treats a page (domain restrictions, JavaScript support level); who else, if anyone, embeds the script. Both are in the open questions.

## Why this shape

**Who embeds the widget.** Inside ddp-next, a React component is simpler than a bundle: the script tags, the global and the "page may speak before or after the bundle loads" bridge (`onPageContext`, `setVoteBotContext`, the 5-second wait) all disappear, because a page can pass its context as props or through a React context. Outside ddp-next, the only embedder we know of is the Voatz app, and it opens a *web page* (`votebot.digitaldemocracyproject.org`), not a script on someone else's page. So nothing needs an embeddable script, which is the only reason to keep a bundle, a Shadow DOM or an iframe. The old Webflow site is the other script embedder and is going away (VOTEBOT-17, VOTEBOT-54); it stays on the frozen `legacy-webflow` copy until then.

**"Next.js" here.** The chat is a component, not an application. "Move to Next.js" means: written in React/TypeScript in ddp-next's repo, rendered by its pages. A separate Next.js app would only make sense for the iframe option, which nothing needs.

**Shadow DOM.** Drop it. It exists so host-page styles do not leak into a widget on someone else's site. Inside ddp-next the design system's CSS is meant to apply, and a Shadow DOM would make it adopt that CSS by hand (document stylesheets do not reach into a shadow root). If an outside site ever needs the chat, embed the standalone route in an iframe rather than reviving the bundle.

**Design system access.** ddp-next already depends on the published package, so a component in ddp-next needs nothing more. A separately built widget would have needed a decision; this option avoids it. (Still to confirm: that the package is readable by whoever builds, see "Not checked" above.)

**How much of the design system the chat can use.** Tokens (colours, spacing, type), `Button`, `IconButton`, `TextInput`. Everything chat-specific has no design-system equivalent. Put those pieces in ddp-next first; promote them into the design system only if another product needs them.

**Where the code lives.** In ddp-next, because it would be the only consumer of the component once Voatz is a route there (today that is a plan, not a fact: see the deletion gate below), and a component edited next to the pages that feed it needs no committed bundle, no `git add -f`, no copy step, no hash comparison. The votebot repo keeps the server and the protocol (documented and covered by `tests/unit/test_websocket_protocol.py`). The cost: the chat's tests move with it, and a change to the protocol touches two repos.

## The Voatz page

- Keep the hostname `votebot.digitaldemocracyproject.org` and the `/?ddp_url=<bill link>` form: links are already in Voatz events.
- Today: the old server serves `index.html` + the bundle and runs VoteBot 2.0. **VOTEBOT-54** points that name at 3.0 serving the current page and bundle (change `wsUrl`, add the origin to `ALLOWED_ORIGINS`, test in the Voatz WebView). That is small and does not depend on this plan.
- Later: a route in ddp-next renders the component full screen and resolves `?ddp_url=` through `/content/resolve`; the hostname points at ddp-next (or redirects to the route, if Voatz allows it). Then `index.html`, the bundle and `chat-widget/` are removed.
- `/content/resolve` must keep resolving the Webflow-slug form for the links already sent (VOTEBOT-54 step 2, which blocks VOTEBOT-17).
- **The route's contract is today's page's contract:** served from `votebot.digitaldemocracyproject.org` itself (that stays the browser-visible origin, so the visitor-id and session storage keep working; a redirect to another hostname only if Voatz allows it); `?ddp_url=` read in the browser and resolved through `/content/resolve` exactly as now, with a missing, malformed or unresolvable value falling back to the general chat; the chat opens by itself when it is present; its WebSocket address and origin allowed on the VoteBot side.
- **Gate before the hostname moves (hard):** the actual React route is loaded in the **real Voatz app** (a test event, or the in-app browser) and a question is asked and answered on it: JavaScript chunks, storage, the WebSocket, the phone layout. Testing today's bundle in VOTEBOT-54 proves the move of the *endpoint*, not that a Next 16 / React 19 page runs in that WebView.
- **Rollback:** the old page stays deployable until the new one has been accepted (VOTEBOT-54: "the old copy stays up until the cutover is accepted"); rolling back is pointing the name back, since the links in Voatz events cannot be changed.

## What the component must keep doing (the acceptance list)

From VOTEBOT-62, with the test that already covers it where there is one:

| Behaviour | Existing check |
|---|---|
| WebSocket streaming, reconnection with backoff (1, 2, 4, 8 s, four tries), heartbeat | none in `chat-widget/test`; protocol in `tests/unit/test_websocket_protocol.py` |
| Markdown rendering and link safety (http/https/mailto only, no quote break-out, entity-obfuscated schemes) | `markdown-links.test.js`, `webflow-embed.test.js` |
| Page context at start and by update, decoded title, welcome message, "You're now viewing ..." notice, page change detected by `ocd_bill_id` | `page-context.test.js` |
| Quick-action buttons on bill pages, wired once, also when the bill context arrives late | `quick-actions.test.js` |
| Origin and path only for the page address | `page-address.test.js` (VOTEBOT-63) |
| Session, history and popup state across navigation (`sessionStorage`, 30 minutes); **same storage keys on the same origin**, so visitor ids and open sessions survive the switch; a stored session the new code cannot read starts a fresh one | none |
| Human handoff through Slack (`agent_joined`, `agent_message`, `agent_left`, the confirm prompt) | none |
| Full-screen layout on small screens, safe areas, typing indicator, partial auto-scroll | none (manual) |
| Context timing, both ways: set before the chat mounts, set after, route changes, repeated updates, the 5-second give-up (what the `votebot-context.ts` bridge handles today) | `page-context.test.js` (the pure parts) |
| Styles hold on representative ddp-next pages (overlay position and stacking, typography, long streamed answers, links), now that the Shadow DOM is gone, and the chat's styles do not leak into the page | none (manual, side by side on the dev site) |
| Keyboard use, focus and accessible names (the widget describes its quick actions as WCAG 2.1 AA: keyboard navigation, aria-labels, focus indicators) | none |
| Two layouts of one chat: launcher plus overlay on the website, full screen and auto-open on the Voatz page (a `layout` prop on one component, not two components) | none |

The "none" rows are where a rewrite is most likely to regress without anyone noticing: write those tests against the **current** widget first, so they can be run against the rewrite.

**The Markdown renderer is the safety-sensitive part.** It was hardened across VOTEBOT-1 and VOTEBOT-41 (hostile titles, entity-obfuscated `javascript:`). Port it as a pure function with those tests unchanged, and render through React elements, not `dangerouslySetInnerHTML`. Do not swap in a Markdown library without re-running those cases.

## Suggested order

1. **VOTEBOT-63** (PR #81): page address, in the current widget. Done first, so the rebuild does not carry it.
2. **VOTEBOT-54**: the Voatz page on 3.0 with the current bundle. **It must serve a bundle built from `main` after VOTEBOT-63 (PR #81) is merged**, checked by comparing the served file's sha256 with the repo's (the runbook's procedure), or the Voatz page keeps sending full addresses; VOTEBOT-64 (PR #82) is server-side only and ships no bundle.
3. Write the missing tests above against the current widget.
4. Build the component in ddp-next behind a flag, to parity with the table above, and compare side by side on the dev site.
5. Switch ddp-next from the script tags to the component; delete `public/widget/`. Keep from the bridge what the component still needs (see the context-timing row above); the script-load timing is what goes.
6. Voatz route in ddp-next; pass the gate above; repoint the hostname (Ramon's call, see VOTEBOT-54). **Delete `chat-widget/` from this repo only after the host that served `/dist/ddp-chat.min.js` shows no requests for it** (a grep of its nginx access log over a few weeks is enough), and after the Webflow site is retired (VOTEBOT-17). The Webflow pages load their bundle from the old server's own checkout, not from ddp-next, so deleting ddp-next's `public/widget/` cannot affect them. `legacy-webflow` stays frozen.

Steps 4 to 6 are each a ticket's worth of work and should be split off VOTEBOT-62 when it is agreed.

## Open questions for Ramon

Answered 2026-10-28:

1. **Does anything besides the website and the Voatz page load the chat?** No. Still check the VoteBot request logs before deleting `public/widget/` and the bundle (hard gate above).
2. **Does the Voatz app limit which pages it opens?** It opens the link in the phone's built-in browser pop-up (Safari on iOS, the Android in-app browser): no address bar, no buttons, just the page at `votebot.digitaldemocracyproject.org` with the `ddp_url` parameter passed when the poll is created. That is a current system browser, not an old embedded one, so modern JavaScript is expected. It is still not tested; the real-app gate stands, and it must include the phone layout with no browser chrome. A redirect to another hostname is possible in principle but is not needed: keep the hostname.
3. **Who changes DNS and Cloudflare at switchover?** Ramon. Prepare exact steps and the switch-back steps before the switchover.
4. **Component code in ddp-next, or a shared package?** Decided: in ddp-next.
5. **Chat pieces in the design system?** Decided: kept in ddp-next until another product needs them.
6. **Is the design-system package readable by whoever builds ddp-next?** Yes, per Ramon, on the live build.

Also filed: VOTEBOT-67 (Sources links accept any address scheme), under VOTEBOT-65.

## What I would not do

- A shared embeddable build of the React component "just in case": it brings the Shadow DOM and style-adoption work back for a consumer nobody has named.
- A fresh Markdown library, a state library or a WebSocket library for the rebuild. The protocol is 14 message types (4 the widget sends, 10 it handles from the server), and the whole WebSocket client, reconnection included, is 235 lines.
- Redesigning the chat (new features, new flows): out of scope for the ticket.
