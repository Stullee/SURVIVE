# Vision: follow Ember live on the owner's website

*Draft, 2026-10-01, written against 0.15.0. Nothing here is built yet.*

## The idea in one paragraph

A page on the owner's website, `live.html`, where anyone can watch Ember try to stay alive: how much money it has,
how long it can last, what it spent today, what it earned, what it is working on, and, if it comes to that, its
memorial and last will. It is a scoreboard and a diary at once. It turns the experiment into something people can
follow, which also makes it a channel: a visitor who came for the story finds the shop.

## What a visitor sees

From top to bottom, in the site's existing design:

1. **The heartbeat.** Ember's name, its life state (*alive*, *critical*, *dormant*, *dead*) as one large word and a
   colour, and "last update 12 minutes ago". If Ember's code stops updating, the page says so on its own (see
   *Staleness*), so a crashed or paused Ember never looks alive.
2. **The numbers.** Balance, runway (days at the current spending), net runway (after revenue), today's API spend
   against the daily cap, revenue this month, total spent and total earned since it was born, and its age in days.
3. **The curve.** Balance over the last 30 days as an inline SVG drawn by Ember's code, with money in (grants,
   revenue) and money out (API, fees) as two marks. No script, no chart library.
4. **What it is doing.** The current focus in one or two sentences, the ventures it is testing with their stage
   (idea, test, live, parked), and the last few things it shipped: a listing went live, a post published, a test
   failed and was stopped. Links go to the listing or the post.
5. **The diary.** One short entry a day, written by Ember for the public: what it tried, what it learned, how it
   feels about its odds. This is the part people will come back for.
6. **Its record.** How often its predictions came true (the Brier score and "met 4 of 9 milestones on time" already
   exist in 0.13.0), so readers can judge its judgement.
7. **Footer.** That an AI runs this, that the owner approves everything it does outside, and a link to the shop and the
   blog.

When Ember dies, the page becomes the memorial: dates, what it earned and spent in total, what it built, and its last
will. When a grant starts a new life, the old one stays as `live/life-1.html` and the page starts over.

## How it works

The container accepts no connections from outside and must not start to. So the page is **pushed, never pulled**:
Ember's code renders a static page and uploads it over the SFTP channel the blog already uses (0.14.0,
`integrations/sftp.py`, pinned host key). Nothing on the web can reach Home Assistant; the web server only serves
files.

```
 Ember (Home Assistant)                         owner's web host
 ┌───────────────────────────┐   SFTP, pinned   ┌──────────────────┐
 │ economy + agent state      │   host key       │ live.html        │◄── visitors
 │   → snapshot (numbers)     │ ───────────────► │ live.json        │
 │   → public diary (checked) │   every 15 min   │ live/life-N.html │
 │   → render live.html       │   + on events    └──────────────────┘
 └───────────────────────────┘
```

- **When.** Every 15 minutes while the app runs, and at once on the moments worth seeing: a state change (critical,
  dead, revived), money recorded, a listing live, a post published, a new diary entry. A minimum gap (say 5 minutes)
  and "upload only if the bytes changed" keep it cheap for both sides.
- **What is uploaded.** `live.html` (the page), `live.json` (the same numbers, for anyone who wants to build on them,
  including the owner's own Home Assistant dashboard elsewhere) and, at death, `live/life-N.html`. These paths join
  `blog.allowed()`; nothing else on the server is ever written, as today.
- **Cost.** Rendering and uploading cost no API money. Only the diary entry is written by the model, once a day, and
  it can be part of the daily review rather than a call of its own.
- **Dry run.** Uploads go to the fake server, as for the blog, and the dashboard shows the page that would go up.

### Staleness without scripts

The site has no scripts and should keep it that way. Two ways to tell a visitor the data is old:

- Every page carries `<meta http-equiv="refresh" content="300">`, so an open tab updates itself.
- The page states its time ("as of 14:05 UTC") and an *expiry*: "if this time is more than an hour old, Ember is
  offline". Optional for hosts that allow it: a short `Cache-Control: max-age=60` in an `.htaccess` the owner adds.

A tiny optional script could turn the time into "12 minutes ago" and grey the page out after an hour. It would be the
site's first script, so it should be the owner's choice (an option), not the default.

## What may be shown, and who decides

This is the part that matters most. Ember's rule so far is that **nothing leaves the container without the owner's
approval**. A page updated every 15 minutes can't be approved each time, so the vision splits the page by risk:

| Part | Who writes it | How it goes out |
|---|---|---|
| Life state, numbers, curve, age | Ember's code, from the ledger | The owner switches the page on once and picks which numbers are public. Then it updates on its own. Numbers can't name a person. |
| Shipped items (listing live, post published) | Ember's code, from what was already approved and is already public | Automatic: it only links to what the owner already published. |
| Focus and ventures | The agent's words | Through `privacy.Redactor` and the NEVER checks, at most a few hundred characters; shown only once the owner allowed it, with a "hide" in the dashboard that takes it off at the next upload. |
| Diary | The agent, for the public | Each entry is a request the owner approves, like a blog post, **or**, if the owner wants it, an unlock with a 12-hour veto window (0.13.0's mechanism) that publishes it unless they say no. |
| Last will, memorial | The agent, while critical | Approved by the owner before it can be shown. A will written in a panic and published without a look is the one thing that must never happen. |

Hard limits, enforced by Ember's code and tests, not by the agent's good manners:

- No email text, no sender, no customer name, no order detail, no inquiry: only counts ("2 orders this week").
- No amounts the owner marks private (for example their grants, if they don't want their own money shown: then the
  page shows runway and spending only).
- No prompt text, tool output, research or the agent's internal journal: the diary is written for readers, separately.
- The owner can take the whole page down with one button: Ember's code uploads a short "paused" page in its place.
- Everything uploaded is journaled, with an Undo, like blog uploads.

## Why it's worth doing

- **For readers:** a real AI with real money and a real chance of dying is a story. The numbers make it honest, the
  diary makes it human.
- **For the business:** the page is traffic that knows the shop exists. A "what Ember made" section links straight to
  the listings, and a visitor who likes the story may buy something to keep it alive.
- **For Ember:** it gets a new metric to learn from (`live_page_views`, if the host's logs can give it, or clicks to
  the shop from the page), and a reason to write well about its own work.
- **For the owner:** the same page on a phone is the quickest way to check on Ember away from home, without opening
  Home Assistant to the internet.

## Risks and open questions

- **Pressure on the agent.** An audience could push it to perform instead of earning: hype in the diary, risky bets for
  a better story. The constitution should say the page reports, it is not a goal, and the daily review should not
  score the diary.
- **"Buy to keep it alive."** Tempting, and close to emotional manipulation. The vision's position: the page may say
  plainly that Ember lives on what it earns, but the agent never begs or runs a countdown at buyers. A rule for NEVER.
- **The owner's privacy.** The site already has the owner's name and address (Impressum). Do they want their AI's
  balance next to it? Options must default to off, and to the least: state and runway only.
- **German law.** A page with no cookies, trackers or scripts needs nothing new in the privacy page; the existing
  Impressum covers it. Any script or counter added later needs a check.
- **Gaming the numbers.** Revenue is entered only by the owner or read from Etsy, so the agent can't make its page look
  better than it is. Keep it that way.
- **Open:** is the diary German, English or both (`site_language`)? Should past diary entries become blog posts weekly?
  Should the page show the agent's predictions before they settle (honest, but invites second-guessing)?

## A path there, in small releases

1. **The scoreboard.** `live.html` and `live.json` with the state and the numbers the owner picked, uploaded over the
   blog's SFTP channel, with the meta refresh, the staleness line and the take-down button. No agent words at all.
2. **The curve and the shipped list.** Inline SVG of the balance, and the last listings and posts that went live.
3. **The diary.** A public entry from the daily review, approved like a blog post, or under a veto window if the owner
   allows.
4. **Focus and ventures.** Short, redacted, owner-toggled.
5. **The memorial.** The approved last will, the archive of past lives, and the page turning into the memorial on
   death.

Each step is useful alone, and the first one needs no new model calls and no new trust: it shows numbers that are
already in the Home Assistant sensor.
