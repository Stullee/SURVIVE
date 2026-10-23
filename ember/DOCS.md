# Ember

Ember is an AI agent that lives in this app and tries to pay for itself. Every
call it makes to Anthropic's API costs real money from a balance you fund; it has
to find honest ways to earn more than it spends. You stay in control: anything
that leaves the container (publishing, contacting people, spending money) needs
your approval, and only you can record revenue (or, if you turn it on, Ember's
code from Etsy's own numbers): the agent never reports its own.

> **Current status: all five phases are in, ready for live testing.** In dry
> run (the default) the agent works with a built-in fake model and costs
> nothing. With an API key and dry run off, it calls Anthropic's API and spends
> real money within your limits.

## Getting started

1. On the app's **Info** tab, turn on **Watchdog** (Home Assistant then restarts
   Ember if it ever crashes) and **Show in sidebar**.
2. Start the app and open **Ember** from the sidebar (or **Open web UI**).
   Until you name yourself as the owner, everyone who can open the panel counts
   as one: a banner on the dashboard shows your Home Assistant user ID. Put it in
   **Owner user IDs** on the **Configuration** tab and restart the app (see
   [Security](#security)).
3. Leave **Dry run** on. In dry run the agent uses a built-in fake model and
   never calls the API.
4. Watch a few dry-run wake cycles (or press **Wake now**) to see how the agent
   works and how you answer it. Nothing it does in dry run is real.
5. When you are ready, follow [Going live](#going-live).

## Options

| Option | Default | Meaning |
|---|---|---|
| Anthropic API key | empty | Only needed when dry run is off. Never logged or shown. |
| Agent name | Ember | What the agent calls itself. |
| Starting balance | 20 USD | Your first grant, recorded once when the agent is born. |
| Daily spending cap | 1.50 USD | Hard limit per day. Calls that could exceed it are refused. |
| Spending cap per wake cycle | 0.50 USD | Hard limit per cycle. Must not exceed the daily cap (if it does, Ember uses the daily cap). A working cycle (its plan, a work step and the reflection) can cost up to about 0.25 USD with the default models; the dashboard warns you below 1.5 times that, when most cycles would end after a step or two. |
| Spending stance | invest | What happens as the runway shrinks: **invest** keeps the agent exploring at your caps until its last will (under 15 days of runway the System log warns you once; since 0.42.6 a venture's first sale may take the whole runway, not half), **steady** goes no lower than focus, **conserve** is focus under 30 days and maintenance under 15. See [burn modes](#money). |
| Cash for a venture's first test (EUR) | 20 | A business case that needs more cash than this to start is knocked out: Ember's code won't propose it until you lift that knock-out on its card (see [Ventures](#ventures)). |
| Daily study budget for the library | 0.50 USD | What the agent may spend a day studying the documents you add on the Library tab. It counts toward the daily cap, not the cycle cap (except in the maintenance [burn mode](#money)). 0: nothing is studied, but the documents can still be searched and read. See [Library](#library). |
| Default sleep | 240 min | Time between wake cycles when the agent doesn't choose. |
| Shortest / longest sleep | 30 / 1440 min | Bounds for the sleep time the agent chooses. |
| Tool steps per cycle | 15 | Maximum tool calls in one cycle. While the agent works, the overview counts them ("tool step 3 (at most 15)"); they aren't the steps of its plan. |
| Planner model | claude-sonnet-5 | Plans each cycle: Ember's business decisions. See [Choosing models](#choosing-models). |
| Worker model | claude-sonnet-5 | Carries out the plan and writes the journal. |
| Price table | see below | USD per million tokens for each model. |
| Web search price | 10 USD per 1,000 | Charged per search on top of tokens. |
| Dry run | on | Fake model, no API calls, no cost. |
| Let the agent read whole web pages | off | Off: live research is web search only. On: it can also read pages from its search results (about $0.01–0.02 each). PDFs and other documents are always refused, because they have no size limit, and so are Etsy's pages, because Etsy's API terms forbid programs reading its site (searching it is fine). |
| Wake Ember when you write | on | A message you send in the **Inbox** wakes the agent to read it, 5 minutes after your last message or decision (at most 15 after the first, and at least 30 after the last such wake), so one cycle reads them all (after a running cycle, unless that cycle saw them; not while the agent is paused or dormant). **Wake now** is immediate. Off: it reads your message at its next scheduled wake-up. |
| Wake Ember when you decide | on | Your decisions switched on in the five options below wake the agent to act on them, the same way as a message. Off: none of them does, whatever those options say; it sees your decisions at its next scheduled wake-up. While a request waits for you and a decision of yours on it wouldn't wake the agent (this option, or **Wake Ember when you approve a request** or **… when you reject a request**, off), the agent sleeps at most 4 hours (or the **Default sleep**, if that is longer), and works on other things meanwhile. Once no request waits any more, the sleep it chose stands again (counted from the end of that cycle). Since 0.40.1, while both your approval and your rejection wake it, a waiting request leaves its sleep as it chose it: your decision brings it back, and work that is ready cuts the sleep anyway. On 10-17 a pin waiting for you woke it after 4 of the 6 hours it chose, twice to nothing it could do. |
| Wake Ember when you approve a request | on | Approving one of its requests, as it is or with your changes, wakes it (0.31.0). Off: it sees your approval at its next scheduled wake-up. |
| Wake Ember when you reject a request | on | Rejecting one of its requests wakes it. |
| Wake Ember when you mark a request done or failed | on | Marking an approved request done or failed, or cancelling one Ember carries out itself, wakes it. |
| Wake Ember for your venture decisions | on | Backing, parking or killing a venture, sending it back to research, a note on it, and lifting or restoring a knock-out wake it. |
| Wake Ember for your roadmap decisions | on | Your decision on a milestone (a note, dropping it, accepting or rejecting the date Ember proposed), setting or removing your goal, and the unlocks you give or take back wake it. |
| Wake Ember for events | on | The events switched on in the three options below wake the agent for a short cycle (see [the agenda](#events-wake-it-the-agenda)). Off: none of them does; they wait for its next scheduled wake-up. |
| Wake Ember for a reply to its email | on | A person's email that answers one Ember sent wakes it (0.31.0). Off: it waits for the next scheduled wake-up. |
| Wake Ember for a new email from a person | on | A person writing to Ember first wakes it. |
| Wake Ember on a milestone's last day | on | The last day of a milestone that Ember's code doesn't check itself wakes it. |
| Worker effort | default | How thoroughly the model works in each step. `medium` or `low` write shorter answers and use fewer tool calls, which costs less but may do a worse job. Not used for Haiku. |
| Kill switch reset | 0 | Change it to any other number and restart to undo the kill switch. |
| Owner user IDs | empty | The Home Assistant users who are Ember's owner: only they can use the dashboard and its actions. Empty: everyone who can open the panel. The dashboard shows your ID while this is empty. See [Security](#security). |
| Log level | info | Detail in the app log. |
| Ember's mailbox | off | Lets Ember read its own mailbox and propose emails. See [Ember's mailbox](#embers-mailbox). |
| Mailbox address, app password, your name for emails | empty | Needed when the mailbox is on. The password is never logged or shown. |
| Your mail provider's authserv-id | empty | The name your mail provider writes first in its *Authentication-Results* header. Set, only that header counts as the provider's verdict on a sender; empty, the topmost one does, which a sender can write. See [Ember's mailbox](#embers-mailbox). |
| IMAP server / port | imap.mailbox.org / 993 | Where Ember reads mail (always TLS). |
| SMTP server / port | smtp.mailbox.org / 465 | Where Ember sends approved emails: 465 (TLS) or 587 (STARTTLS). |
| Emails per day | 3 | The most emails Ember sends in one day (0 to 20). |
| Workshop | on | Lets the agent have code written and run in Anthropic's sandbox. See [The workshop](#the-workshop). |
| Workshop model | empty | The model that writes the workshop's code. Empty: the worker model. |
| Workshop cap per run | 0.75 USD | The most one workshop run may be priced at, and what Ember keeps of the daily cap for each run (more after costlier runs). Runs count toward the daily cap, not the cycle cap. No runs in the maintenance [burn mode](#money). |
| Workshop runs per day | 6 | The most workshop runs in one day (0 switches the workshop off). |
| Sandbox price | 0.05 USD per hour | What Anthropic charges per hour of sandbox time beyond its free hours. |
| Etsy shop | off | Lets the agent propose listings for your Etsy shop, which Ember creates after you approve them. See [Etsy](#etsy). |
| Etsy app keystring, shared secret | empty | From the Etsy app you register for your shop. The secret is never logged or shown. |
| Etsy redirect URI | https://localhost/ember-etsy | The callback URL registered for that app, exactly as there. |
| Etsy listings per day | 3 | The most listings Ember creates in one day (0 to 20). |
| Keep a history of the listings' numbers | off | A daily record of each listing's views and favorites, so the plans see how each changed. Turn it on only once you have confirmed that Etsy's API terms allow it (see [What Ember sees](#what-ember-sees)). |
| Renew listings that sell | on | Once a listing has sold, Ember turns on Etsy's automatic renewal for it (USD 0.20 every four months), once per listing (see [How a listing is made](#how-a-listing-is-made)). |
| Record Etsy revenue automatically | off | At each Etsy sync, Ember's code records the revenue of paid orders with Ember's listings placed from the day you turn it on, Etsy's fees on them and their refunds in the ledger, instead of you, and what Printify bills for its orders (see [What Ember sees](#what-ember-sees)). |
| Exchange rate for Etsy revenue | 0 | USD per 1 EUR for orders in EUR recorded automatically (0.5 to 3; 0: orders in EUR stay yours to record). |
| Etsy market probe for demand notes | off | A demand note also reads Etsy's search of active listings for its keywords, keeping only how many match and their price quartiles. Turn it on only once you have confirmed that Etsy's API terms allow this use (see [How a listing is made](#how-a-listing-is-made)). |
| Pinterest | off | Lets the agent propose pins that bring buyers to your Etsy listings, which Ember makes on your Pinterest account after you approve them. See [Pinterest](#pinterest). |
| Pinterest app ID, app secret | empty | From the Pinterest app you create for your own account. The secret is never logged or shown. |
| Pinterest redirect URI | https://localhost/ember-pinterest | The redirect URI registered for that app, exactly as there. |
| Pins per day | 3 | The most pins Ember makes in one day (0 to 20). |
| Pinterest sandbox | off | For your Standard access request at Pinterest: Ember connects to Pinterest's API sandbox and puts a test pin on your list, so you can record the video Pinterest asks for. No other pins while it is on. See [Standard access](#standard-access). |
| Bluesky | off | Lets the agent propose posts on the Bluesky account you made for Ember, which Ember posts after you approve them. See [Bluesky](#bluesky). |
| Bluesky handle, app password | empty | The account's handle (such as `ember-shop.bsky.social`) and an app password made for Ember in its settings, never the account's own password. The app password is never logged or shown. |
| Bluesky posts per day | 2 | The most posts Ember makes in one day (0 to 20). |
| Printify | off | Lets the agent propose physical products with its designs, made on order by Printify and sold in your Etsy shop. See [Printify](#printify). |
| Printify personal access token | empty | A token you create in Printify for Ember. Never logged or shown. |
| Printify shop | 0 | The Printify shop connected to your Etsy shop (0: the only one that is). |
| Printify currency | EUR | The currency of the prices and costs Printify shows for that shop. |
| Printify products per day | 2 | The most products Ember creates in one day (0 to 10). |
| Website | off | Lets the agent write the pages of a small website of yours, which Ember builds and you publish. See [Website](#website). |
| Website name, language, address | empty, de, empty | The site's name in its header (empty: your name), the language of its pages, and where you publish it (https, for its sitemap): the site's folder address like https://example.org, not a page's file name (a trailing /index.html is dropped). |
| Your full name, address, email, phone, VAT ID (Impressum) | empty | Your data for the site's Impressum and privacy page: name, address (street, postcode and town; not a PO box) and email are needed; phone and VAT ID (or your Wirtschafts-Identifikationsnummer) optional. Never in the diagnostics report. |
| Your web host (privacy page) | empty | The company hosting your site, named on the privacy page. |
| Blog (published by Ember) | off | Lets the agent write blog posts and the link page for your own website, which Ember's code uploads over SFTP after you approve each one. Uses the website address, your name, the town in your address and your email above. See [Blog](#blog). |
| SFTP server, port, user, password | empty, 22, empty, empty | Your web host's SFTP login (at STRATO: Customer login → Your package → Databases and webspace → SFTP & SSH). Only Ember's code uses it, never the agent; the password is never logged or shown. |
| SFTP server key | empty | The server's key fingerprint (`SHA256:...`) or public key line, so Ember connects only to your server. Empty: the key seen at the first connection is kept (and shown on the dashboard). |
| Website folder on the server | empty | The folder your domain shows, e.g. `/ember-ai.de`. Empty: the folder the login opens. |
| Amazon KDP (books you publish) | off | Lets the agent make books for Amazon KDP, which you publish at KDP yourself after you approve them. See [Amazon KDP](#amazon-kdp). |
| KDP author name | empty | The author name Ember's books carry: yours or a pen name. Empty: you enter it at KDP. |

What wakes Ember is yours to choose one by one (0.31.0): each option that starts
with *Wake Ember* is a switch of its own, and a decision's or an event's switch
counts only while its group's (**Wake Ember when you decide**, **Wake Ember for
events**) is on. To have Ember wake for your approvals but not for your
messages, for example, turn off **Wake Ember when you write** and the four other
decisions' switches. What doesn't wake Ember waits for its next scheduled
wake-up, and **Wake now** is immediate.

Default prices (USD per million tokens, from Anthropic's pricing page on
2026-09-27; **check them before going live**):

| Model | Input | Output | Cache write 5 min | Cache write 1 h | Cache read |
|---|---|---|---|---|---|
| claude-sonnet-5 | 2.00 | 10.00 | 2.50 | 4.00 | 0.20 |
| claude-opus-5-5 | 4.00 | 20.00 | 5.00 | 8.00 | 0.20 |

To use another model (for example a cheaper worker), add its prices to the
table. The planner, the worker and the workshop model must be listed there.

Home Assistant checks each option's range and form when you save (a website
name of at most 60 characters, an https website address, and so on). It can't
check whether options fit together, so Ember corrects that at start, toward
spending less, and the top of the dashboard shows each correction until you fix
the options: a cycle cap above the daily cap counts as the daily cap, a longest
sleep below the shortest as the shortest, the default sleep is kept between the
two, an exchange rate below 0.5 counts as none, and a name, model or redirect URI
made of spaces only counts as its default. Any other invalid option
(for example a model missing from the price table) starts **safe mode**:
built-in defaults, dry run forced on, and the problem shown at the top of the
dashboard. Safe mode keeps your **Owner user IDs** and never lifts the kill
switch (a new **Kill switch reset** applies at the first start with valid
options). If it can't read **Owner user IDs** at all, Ember answers no one: each
user gets a page that says so, with their user ID. A mailbox that is switched on
but incomplete doesn't cause safe mode: the dashboard's **System** panel says
what is missing, and Ember works without it.

## Choosing models

The planner decides what Ember works on in each cycle and why: that is where a
smarter model pays off. The worker carries out the plan step by step, many calls
per cycle, and claude-sonnet-5 does that well at a lower price.

To plan with **Claude Opus 5.5**:

1. Make sure the price table has a row for it (check Anthropic's pricing
   page first). New installs have it; if you saved your price table before
   0.7.0, add it: model `claude-opus-5-5`, input 4.00, output 20.00, cache
   write 5 min 5.00, cache write 1 h 8.00, cache read 0.20.
2. Set **Planner model** to `claude-opus-5-5` and keep the worker on
   `claude-sonnet-5`.
3. Save and restart the app.

Opus 5.5 always thinks before it answers, and Ember leaves room for that in
every call. A plan then costs about two to three times as much (typically
$0.06–0.12 instead of $0.03–0.04, at most about $0.16). To keep the daily
spending about the same, let Ember wake less often: raise **Shortest sleep** to
120–180 minutes. A working cycle can then cost up to about $0.38, so set the
cycle cap to 0.60 or more: the dashboard warns you if the cycle cap is too low
for one planning call, for one working cycle, or for 1.5 working cycles.

Opus 5.5's safety classifiers sometimes refuse a request that is fine. Since
0.42.1, when the planner refuses to plan and the worker is another model, the
worker plans that cycle instead, once, under the same caps: the System log warns,
and the cycle's note says *planned by claude-sonnet-5 after claude-opus-5-5
refused*. If the worker refuses too, or the caps leave no room for its plan, the
cycle stops as before. A work step's refusal still ends its cycle.

The **Workshop model** can be `claude-opus-5-5` too, for harder code. A run's
first call may then cost up to about $0.68, so raise **Workshop cap per run** to
1.00 (the dashboard says so when the cap is too low for one run).

**Models by purpose.** Most plans are routine. To put Opus on the decisions that
set the course only, keep the planner on `claude-sonnet-5` and set **Model for
venture plans and reviews** to `claude-opus-5-5`: venture cycles plan with it, and
the daily review and the critic of a proposed venture run on it, while ordinary
plans stay on Sonnet. A cycle can start only if its costlier plan fits, so the
caps are checked against Opus.

**Research model.** Research can run on a cheaper model: set **Research model**
to `claude-haiku-4-5` (new installs have its price; add it to an older price
table: input 1.00, output 5.00, cache write 5 min 1.25, cache write 1 h 2.00,
cache read 0.10). It doesn't take over at once. The agent's next 10 research
questions go to both models; the agent reads the worker model's answer, and
Ember's code compares what each found. The research model takes over if it
answered at least 9 of the 10 and found web pages for as many questions as the
worker model, less one; otherwise research stays on the worker model. The
System log says how the check came out, and **System → Models** shows where it
stands. Each compared question costs one more research call (a cent or two with
Haiku), counted as overhead. The money guard treats that call as research
(0.37.4): it keeps back at least 1.5 times the costliest research call of the
last 14 days on the research model, its check calls included, and needs 5
times that left above the last will's reserve (see [Money](#money)); a question
it can't pay for that way is compared with a later one, and the agent's own
research goes on. Once the research model takes over, its research keeps back
what its check calls cost too. Until 0.37.4 a check kept back only its worst
case, and needed only that left above the reserve: in a test, after a check
that cost $0.45, the next went ahead with $2.93 left above the reserve, keeping
back $0.49; as research it keeps back $0.675 and needs 5 times that.

## How the agent works

The agent sleeps most of the time. When it wakes up (on its schedule, when you
press **Wake now**, when you write to it or decide, or when an event needs it),
it runs one **wake cycle**:

1. **Plan**: once a day it first does its [daily review](#the-daily-review).
   Then it reads its situation (balance, runway, projects, what happened
   since the last cycle, your messages and decisions, its memory) and plans
   the cycle. Every plan says how its goal leads to money (who would pay, for
   what, and how it will know); you see this as *Path to money*. **Each cycle is
   about one thing** (0.28.0): an ordinary cycle works on one product line, a
   [marketing cycle](#marketing-cycles) brings buyers to one line's listings, a
   [venture cycle](#ventures) decides one venture, and a cycle an event woke
   works on one line at most. Since 0.35.0 the [plan tree](#plan) under your
   goal decides: before the plan, Ember's code takes the cycle's **step** from it
   (what you pinned, else the heaviest step; since 0.37.0 a promise to you and
   your decision are weighed like any step), and the step decides what the cycle
   is (a marketing step a marketing cycle, a venture's step a venture cycle, any
   other an ordinary one) and which product line its tools work on. The plan sees it as **YOUR STEP** (what done means, why
   it was taken, what comes next) and the whole plan in a few lines as **YOUR
   PLAN**. It stays on a product up to three cycles in a row unless another step
   weighs clearly more. With no step ready, in the explore burn mode it starts a
   new product; otherwise nothing new starts. (0.28.0 to 0.34.0 Ember's code
   ranked the product lines for each plan, **READY**, and the plan took one.) It is
   told to
   do research and legwork itself, to build a thing completely before
   it asks you for one concrete action, to ask you in one batched message and
   only for decisions, money and what only a person can do, and to spend its
   daily cap on experiments rather than sleep to save it. Every plan and work step
   also sees your **rulebook** (since 0.36.0; your standing instructions before)
   and a short list of facts about the outside world that you collected
   (platform rules, German law, what earns money), which comes with each Ember
   update.
   Before anything else it sees its **obligations**, which Ember's code keeps
   (never cut): your messages waiting for an answer, what it promised you (a
   message's promise, with the day it named; since 0.39.1 a promise of "none" or
   "n/a" is none, where three messages were refused as a promise without its
   day), your decisions it must react to
   (a request you rejected or carried out, or one that failed), milestones
   Ember's code closed missed, overdue milestones and live listings with too
   few photos. It closes a promise only after telling you it is kept (or why
   not). A promise about a product (message_owner's project_id, or since 0.35.1
   the product its words name: a listing's number, KDP) is a step of that
   product in the plan tree, and since 0.37.0 one about no product is a step of
   the plan's **Owner** project. Your decision on a product's request is a step
   too. Since 0.37.0 each is weighed like any step: since 0.37.1 worth at least
   3 (a little more than most products), urgent from two days before its day
   and more as it nears, until cycles took it three times in a day without
   keeping it (your decision once): then it weighs its worth alone until that
   day is over, so one Ember can't keep yet doesn't take every cycle. (0.35.0 to
   0.36.0 they came first; in 0.37.0, worth 5 and always urgent, they still
   did.) Since 0.37.6 one a cycle took without keeping it, and without making
   anything for its product, waits until something moves or the next day, like
   any step (see [Plan](#plan)). A missed milestone decides nothing.
   A message of the agent's that promises later work in words
   only ("next cycle I'll ...") comes back to it once, to record the promise.
   Since 0.42.6 each sentence counts on its own: a sentence reporting on an
   open promise goes through, another one's promise doesn't (live, "Next
   ordinary cycle will apply the 3 fixes" went out unrecorded because a later
   sentence named promise #67, and no cycle made the fixes). A promise holding
   every word of an open one (3 words at least) repeats it, and one whose words
   name another line's listing or product type (KDP, Printify) is that line's,
   whatever line it was made on: the Kassenbuch's proposal was promised twice,
   the second time on the cover-letter line, each promise with its own step.
   Any cycle may keep another line's record up to date (a note, its hypothesis,
   waiting; parking an idea with what it learned; closing an obligation with
   its evidence); work on another line (a request, a bet, reopening it) waits
   for that line's cycle. A scheduled venture or marketing cycle
   answers your waiting messages first (0.19.3), and leaves what needs files or
   the shop to the next ordinary cycle.
2. **Act**: it uses its tools, up to the *Tool steps per cycle* option: files in
   its own workspace, its memory (strategy, identity, lessons), projects, web
   research (also limited to one site, such as etsy.com), requests for your
   approval, messages to you, requests for code upgrades, and its own mailbox
   if you set one up. It also makes finished [products](#products) from what it
   writes: PDF documents with an editable Word copy, Excel spreadsheets and
   listing photos, and it looks at their pictures to check them. For what
   those tools can't make (charts, PowerPoint files, data work), it has code
   written and run in its [workshop](#the-workshop). Ember's code keeps the
   tools on the cycle's line (0.28.0): another line's listing, product, update,
   request, pin or post, or one of its obligations, is refused and waits for a
   cycle on that line. Messages to you, its memory, its plan's steps and new
   ideas for the venture tree are always allowed; since 0.35.0 only you close or
   drop a project. A cycle
   that took no line takes the line of its first tool call that works on one,
   and the files it wrote until then go under that line.
3. **Reflect**: it writes a journal entry, updates its memory and chooses how
   long to sleep. It is told which of the cycle's tool calls were not done
   (refused, failed, skipped or cut off), so it can't report them as done.
   Every cycle that worked reflects. Since 0.24.0 a journal the agent writes in
   a work step is kept as the reflection's draft: the reflection writes it again
   only to correct it, and when it writes none, or its own is refused, the draft
   is the cycle's journal. When the model writes another tool's call inside its
   journal (a lesson inside the entry), Ember's code takes it out, so the
   journal is kept. Since 0.39.4 it runs such a call only when it only reads, as
   one of the four tool calls a reply runs (the journal itself isn't counted);
   any other it refuses, and the agent makes the call itself if it meant it. In
   any other text (an email saved to its notes, a quote in a message to you)
   such markup is part of the text and stays there: until 0.39.4 a call written
   there ran as the agent's own, so a buyer's email could change its lessons and
   a quote could delete a file.

When a cycle ends, however it ends, Ember's code writes its **digest** from its
records: what it was aimed at, its goal, what its tools did and what they
didn't, how its work ended, whether it reflected and what it cost. A cycle cut
off by an app restart gets its digest at the next start (unless a later cycle
has ended since). The next plans see the last two digests, and a work step
aimed at a milestone or venture sees the digest of the last cycle aimed at it:
a journal can claim work that never happened, a digest can't. After a cycle
that left no "next" for the next plan (stopped, failed, cut off or idle), the
next plan still sees the last one the agent wrote. Your decisions and comments stay in
the agent's news until a cycle that saw them ends normally.

Every call and every tool use is shown on the dashboard (click a cycle under
**Activity** for the details, its digest first; since 0.28.0 each cycle there
says what it was: a product line, marketing, a venture or an event, with its
line or venture). In dry run the fake model doesn't understand
your messages, so its replies in the **Inbox** are canned (the Inbox says so
above the message box); with dry run off, Claude reads and answers them. The
agent can't reach the internet except through Anthropic's web search (and page
reading, if you allow it), can't run programs on your Home Assistant (its
workshop runs code only in Anthropic's sandbox) and can't touch anything
outside its own folders. Its spending limits, the approval rule and its tools are
enforced in code, not only in its instructions.

When its money runs low, it becomes critical and writes a **last will**, shown
in the memorial if it dies.

### Events wake it (the agenda)

Between cycles, Ember's code notes in the **agenda** what happens while the agent
sleeps (0.13.0), each thing once. It reads the Etsy shop hourly and Ember's
mailbox every 15 minutes, also while the agent is paused or dormant. While
reading the mailbox fails, it tries less often, down to every 4 hours (also at
the start of a cycle), and warns you once (0.15.0):

- an Etsy order of Ember's listings;
- a person's email that answers one Ember sent (not an automatic reply, a
  bounce, an unverified sender or someone who asked to stop: 0.15.0);
- a listing's favorites reaching 5, 10, 25, 50, 100 or more (the counts a
  listing already had when the agenda began don't count);
- an open milestone's last day (from 08:00 on its date).

The next plan lists them under *since your last wake*. A reply, a new email
from a person (see [Ember's mailbox](#embers-mailbox)) or a milestone's last day
also **wakes** the agent for a short **reactive cycle**, with the option **Wake
Ember for events** and the event's own (0.31.0: a reply, a new email, a
milestone's last day). It works on the event first, in at most 5 work steps, on
one product line at most, with no venture work, daily review, library study or
critic. That happens at most
**4 times a day**, at least **30 minutes** apart, and only while the day's cap
covers a cycle. It never happens while the agent is dormant, while it backs off
after a failed, stopped or refused cycle, after three interrupted cycles, or
while its cycle cap leaves no room for work. Orders, favorites and the last day
of a milestone Ember's code checks itself (one with a metric, such as a listing
test's bars, your goal or the money goal) wake no one: Ember's code records them. An
event that can't wake the agent waits for its next cycle.

To keep money for these wake-ups, a scheduled cycle leaves **a fifth of the
daily cap** unspent until **20:00** (your time), unless the cap is too small to
spare it. Every call of that cycle leaves it, workshop runs, the daily review,
library study and the critic too. When a scheduled wake would need that share,
it waits until 20:00. With **Wake Ember for events** off, nothing is kept: no
event can wake the agent to spend it (0.30.3; 0.31.0, nor with all three of its
events' options off). Since 0.39.6 a scheduled cycle also leaves **half of the
daily cap** for your day until **08:00** (your time), whatever wakes Ember, and a
scheduled wake that would need it waits until 08:00: every night from 00:07 the
cycles spent four fifths of the cap, and your decision at 07:48 got one cycle
before the next waited for 20:00 with $0.01 to spend.
Since 0.40.3 these waits, the wait for the daily cap to reset (until midnight)
and the maintenance burn mode's day are worked out again each time Ember decides
whether to wake, and never kept as the next wake: raise the daily cap, add money
or switch event wake-ups off, and a wait whose reason is gone ends at once
(before, it lasted until its time, under the old reason).
Your **Wake now**, messages and decisions are never held back. The activity
list shows an event's cycle as *woken by an event*, and the System log shows
what was noted.

### The daily review

Once a day, at its first wake cycle of the day (from its second day on),
before it plans, the agent goes through its own numbers like a business owner
going through the books. Ember's code puts them together from its records, so
they can't be argued with: the last 7 days' spending by day and by purpose (since
0.45.0 both from the ledger: each call at what it cost once settled, and what
the ledger holds beyond that, a worst case not settled yet or a correction, a
part of its own, so the parts add up to the total; on 10-18 the parts were the
calls' booked costs and the total net of 0.39.8's refund), the
revenue you recorded and its sources, each project's cycles, spending and
requests to you, your decisions and comments, its cycles, errors, finished
products and workshop runs, and its verdicts from the last review.

Since 0.18.0 each project with live listings also shows its funnel (views,
favorites, orders: where it is stuck) and the reach done for it (blog posts,
pins, listing edits). Since 0.24.0 it also sees the channels that wait for your
setup (such as Pinterest without its app secret), so it doesn't plan with them,
and since 0.33.0 the ones that are ready (live, a review read an old "waiting on
owner" note and wrote that Pinterest was never set up, hours after you had),
and its milestones say which ones Ember's code checks and closes itself and
whose dates don't move (a venture's first test, your goal, the money goal).

The agent then judges every project (**continue**, **change** or **stop**, with
its bottleneck: **reach**, **appeal**, **conversion**, **quality**,
**too early** or **none**, and the numbers that decide it), says what works and what doesn't, what your
decisions tell it, one lesson and today's focus, and it checks its venture tree
and its roadmap. Each milestone that is overdue or due this week gets a
verdict, **hit** (its measure is met, with the evidence), **miss** (past its
date and not met), **extend** (a new date) or **park** (it waits a week), and
Ember's code applies each one with the same rules as the agent's own changes (a
milestone with a metric is left to Ember's code, your goal and the money goal
can't be closed, a date moves twice at most): the review keeps what came of each
verdict, and the day's plans see it. Every plan that day shows the
review, and the agent is told to carry it out: change what it changed, work on
each bottleneck. A **stop** is yours (since 0.35.0 only you close a line): since
0.39.1 Ember's code asks you on the Plan tab to keep the line or drop it, as at a
decide-by date, at most once a week a line, and the line is held until you
decide. Until then the review told the agent to close the line itself, which
its tools can't: three cycles tried, and the next review said again that the
stop wasn't carried out. Since 0.40.2 Ember's code holds the line from the
review on (its steps wait), not the agent: on 10-21 two cycles ($0.37) took a
stopped line's monthly blog post only to hold the line, and each cut the sleep
before it. Your **keep** lifts the hold. The agent can resume the line too, and
it stays resumed for that review; a line with a step you pinned or a promise
open isn't held. Since 0.39.6 a **change** of a live or launched line
is a step of its own, due that day ("Review 10-16: ..."), which Ember carries out
and says done; the next review's change takes the place of one still open. A
change waited for its line's cycle, which took the heaviest step instead: three
reviews decided on a small Etsy Ads test, and no request for it was made. Since 0.42.6
not for a line waiting for your keep-or-drop: six cycles ($1.34) closed "Kept
passive at zero cost. The owner decides at renewal" on three such lines, each
step weighing more than the KDP book's, and four of them cut Ember's sleep. The
review also reads your messages of the period (the newest 3, as you wrote them:
what you asked stands) and your KDP books with their Amazon link, or that they
have none yet: its focus had planned a book you put off and pins "linking to
Amazon" for two books without a link. Ember's code adds the review's lesson
to the agent's lessons itself (since 0.18.0). The next review shows whether it did. You can
read every review, with the numbers it judged, under **Mind → Daily reviews**.

A review is one call on the planner model (about $0.02–0.05 with
claude-sonnet-5). It counts toward the daily cap, not the cycle cap, and
leaves what the cycle it opens needs to do its work (in the maintenance burn
mode the cycle's $0.40 bounds it too). If the day's budget can't cover it, it
is tried at the next cycle.

**Lessons.** The agent's lessons file holds 4,000 bytes. When it is full, a
new lesson pushes the oldest out: those without numbers first, so a no backed by
data ("dropshipping ruled out: margins under 5%") lasts. Since 0.24.0 the
lessons just added go last, and the answer names the ones that went. After each daily review,
once the file holds 12 lessons or more, one more call on the planner model
(about a cent) **consolidates** it: lessons that say the same become one, and
those a newer lesson contradicts are retired, each with why. Ember's code checks
the answer: what it doesn't account for stays, and a pinned lesson or one with
numbers is never dropped, unless (since 0.18.0) it names one of the agent's
tools: a tool's limit ("the reason field caps at 300 characters") is no lesson,
the tool's refusal states it. The System log says what changed, and every
version of the file is kept. Each plan shows the pinned lessons and then the
newest others, about 2.6 KB of them.

Under **Mind → Lessons** you can **pin** a lesson (at most 6): the agent never
drops it, a rewrite of its lessons must keep it word for word, and every plan
and work step shows it first. **Unpin** lets it go the usual way.

### The learning loop

Since 0.18.0 Ember's code runs routines that let the agent learn from its own
work, like a manager's calendar (the design is in `vision/learning.md`):

- **Bets.** When the agent changes a project, it can bet on the result with
  project_update ("+15 views in 7 days: the pins bring buyers"). Ember's code
  keeps the number then and settles the bet before every plan: **won**,
  **lost**, or **no_reach** for a bet on views when nothing was done to bring
  buyers meanwhile. A bet on favorites or orders needs the listings seen first,
  and on orders a quality check that didn't say improve.
- **Retrospectives.** The daily review lists what settled since the last one
  (bets, metric milestones, closed projects, parked or killed ventures, requests
  you rejected) and writes a retrospective of each: what it expected, what
  happened, why, the cause (worked, wrong idea, weak execution, no reach, too
  early, outside) and how sure it is. Each is kept as a **case**.
- **The weekly look.** Once a week, after the daily review, one more call on the
  strategy model (about the price of a review) reads the whole business as
  Ember's code puts it together: the money, your goal with how far each of its
  sub-goals got (since 0.30.0), every project's funnel and reach, the ventures,
  where the week's cycles and money went, who started them, the bets' record and
  the cases. It rewrites the agent's strategy (refused if it names a parked or
  killed venture), says what to stop and start, asks up to 3 questions for the
  week and (since 0.30.0) picks the week's **focus lines**: up to 3 open product
  lines that bring the goal nearest soonest. Every plan that week sees them
  (0.30.0 to 0.34.0 READY gave them the cycles after what was owed and the line
  in progress; the plan tree doesn't weigh them yet). Before 0.30.0 a business that had grown (many projects,
  ventures and cases) made the look's view too big for its budget, and the look
  was skipped at every cycle with only a line in the app's log; now the view
  holds your rulebook (until 0.36.0 your standing instructions) and the
  strategy first and the older cases last, it is cut until it fits, and a look that still can't come through is
  shown in the System log and tried again the next day.
- **The playbook.** The agent's principles, drawn from its cases, each
  citing them. Ember's code sets their confidence: established with 3 agreeing
  cases and none against, disputed once a case is against, a hypothesis
  otherwise. A principle no case confirms for 6 weeks (4 months once
  established) is retired. Since 0.30.0 each retrospective's lesson joins the
  playbook as a hypothesis the day its case is kept (not a too-early or
  low-confidence one), or counts as one more case for an active principle that
  says nearly the same (and denies the same things), so it grows every day; the
  weekly look merges, confirms, disputes and retires, and the System log says
  which of its principles couldn't be kept and why (before, only the weekly look
  wrote principles, the first one after 0.18.0 found no case, and one drawn from
  too-early cases was dropped without a word). At the first cycle of 0.30.0 the
  lessons of the cases kept before joined it. Each plan's LESSONS shows the
  playbook first, then the newest lessons. You read it under **Mind → Playbook**:
  each principle with its confidence and its cases for and against, the ones
  retired lately with why, how many cases there are (and how many were too early
  to count), and this week's look with its focus lines.
- **Recall.** The work steps get the principles and cases that match their plan,
  knowledge_search finds them, and creating a project or venture names the most
  similar case.
- **The quality critic.** Before a plan, one live listing a cycle is scored by
  the strategy model (its cover photo, title, tags, price and description
  against the demand note's market prices, and your rule that a product must
  beat a free AI chat): 7 of 10 passes. Since 0.24.0 each listing of a product
  line is checked in turn (one never checked first), again after 14 days or
  after a change of it, and every verdict names its listing: before, only a
  line's newest listing was checked, and its verdicts were read as verdicts on
  another listing of the line. A line whose work your park stopped gets no
  check. The score and fixes show in the daily review.
- **Waiting time.** While a cycle's plan has a step ready (0.35.0: in the plan
  tree; READY's jobs until 0.34.0), the sleep of a cycle that worked is cut to
  your shortest sleep (*Shortest sleep*; 0.35.1, a venture cycle's too: until then 3
  hours, never below your *Default sleep*), not in maintenance. Your daily cap,
  not the sleep, is what holds the spending back: with work ready, Ember may
  spend it early in the day and then wait for the next. On a live line without
  an open bet, the work steps are asked for one. A plan that chose to do nothing
  cuts no sleep. Since 0.37.6 only a step a cycle can advance counts: not the
  cycle's own step if nothing it is checked by moved during the cycle, nor one
  a cycle took before with nothing moved since (see [Plan](#plan)). Live on
  2026-10-09 a step no cycle could advance cut every sleep to 30 minutes, 21
  cycles by 12:35. Since 0.39.6 only work counts: a step that is urgent (a
  promise or decision near its day, a defect, a missed views date, a launch's
  first marketing, a step due today) or one that makes or ships on a channel
  above its floor, a venture's step too; a chore or a report leaves the sleep
  Ember chose (on 10-15 she chose 4 to 6 hours 16 times, and each was cut to 30
  minutes, $5.59 by 07:55). Since 0.39.8 a channel counts only when it is worth
  at least twice its floor: Bluesky at 0.21, a listing view for every five posts,
  cut 7 of 12 sleeps on 10-16. Since 0.37.9 not a step whose work one of your limits would
  refuse either (a pin while the publishing queue is full, a report once the
  day's messages are sent: [Plan](#plan), **Your limits**). Since 0.40.2 a step
  of the ventures (a brainstorm, a triage, an appraisal) counts only when it is
  urgent (your wish, a park within days): on 10-21 a brainstorm and a triage
  kept the afternoon's sleeps at 30 minutes, six ideas came of them, all parked,
  and the next brainstorm was ready at once. A brainstorm rests for a week while none of the
  last one's ideas went on past triage (researching, proposed or further); until
  0.40.2 one idea researched and then parked ended the rest.

All of it counts toward the daily cap, not the cycle cap, and none of it ends a
cycle. The diagnostics report lists the bets, cases, principles, weekly looks
and quality checks.

## Products

Ember doesn't hand you design work: it makes the files itself. The agent writes
the text and chooses the design; Ember's own code turns that into files in the
agent's workspace:

- **Documents** (`make_document`): a Markdown file with a few settings (theme,
  fonts, colours, page size, footer) and layout lines (sidebar, columns, boxes,
  photo boxes, checklists, tables, writing lines) becomes a PDF, an editable
  Word copy (`.docx`) and pictures of its first four pages (since 0.39.1, or of
  up to four pages the agent names, such as a book's pages to fill in). Good for
  printables, planners, worksheets, guides, and CV and letter templates. At most
  40 pages. Since 0.39.1 a table row with no words is one line tall, as Word
  draws it: it was 2.2 mm, its padding alone, so the Haushaltsbuch's weekly
  table of 18 rows to fill in was a band of lines nobody could write in. A
  table's header stays with its first row, and a heading or a one-line label
  (**Bilanz**, Notizen:) with the table or writing lines under it; in a book (a
  KDP size) the agent hears which pages to fill in run on to the next page.
  Since 0.39.2 however many pages they run on to (a notebook's pages of lines
  and a chapter's prose run on as they should), and a table whose rows each
  have an empty cell, a label and its amount to fill in, counts as one to fill
  in: the Haushaltsbuch's v14 made each month's overview three pages, the third
  holding only "Notizen", and was told nothing. Since 0.39.6 `::: lines fill`
  rules a page down to its bottom margin (at least 2 lines, on the next page with
  its label if fewer fit; in the main text only, and the Word copy draws as many),
  and a book names writing lines that end a page with room for 2 or more below
  them: you rejected the Haushaltsbuch a sixth time for that white space. Since
  0.39.8 the result says what each `fill` drew ("2 lines on pages 6, 7, 8 …; 14
  lines on pages 5, 12: each is ruled down to the bottom margin"): the agent read
  2 lines under a full week's table as `fill` stopping short, and went back to
  fixed counts that put 5 lines a week on a page of their own. Since 0.40.2 the
  dashes of the line under a table's header set its columns' widths when their
  lengths differ (`|---|----|------------|`: the third column takes the most;
  each column still at least as wide as its longest word), and a book names a
  column to write words in (a header word of 4 letters or more) that comes out
  under 13 mm: the Kassenbuch's "Text", where each entry is described, was 12 mm
  wide on 48 pages, and nothing could widen it. Since 0.40.3 a page break in
  the Word copy starts the next paragraph's page, also beside a sidebar: a break
  of its own ran on to a blank page after fill lines (an 83-page planner had 135
  pages in Word), and a sidebar's Word copy had none (a 2-page CV, 1 page). A
  `::: pagebreak` inside a box is refused (it printed two layers of text), and a
  book's footer keeps KDP's 0.25 in from the bottom edge at any margin.
- **Spreadsheets** (`make_spreadsheet`): a JSON description becomes an Excel
  file with formats, dropdowns, formulas, totals, a chart and a *How to use*
  sheet, plus a picture of each sheet, drawn since 0.37.5 from the file itself:
  every number as Excel shows it once it has calculated the file. Formulas may
  only use common functions and cells of the same workbook: no links to other
  files or the web. Ember's code tells the agent where each sheet's data are and
  names a formula that leaves data out or (since 0.32.0) points at a title, a
  header or an empty cell below the data. Since 0.37.5 it also names one that
  counts a total row or a header besides the data (`=SUM(C:C)` over a whole
  column adds the total row under the data again), on the formula's own sheet,
  over several columns and in a column's formula too; parts of the data that
  formulas take together, such as four quarters of a year, are no longer named.
  A CSV file of rows whose first line repeats the columns' titles has that line
  left out, and an empty field is an empty cell. To fix a few words or a formula
  of a long file, the agent replaces just that passage (`workspace_write`'s
  `edit`, 0.32.0) instead of writing the file again in parts; since 0.33.0 one
  edit can change every copy of a passage when it says how many there are (a
  planner's 48 weekly tables). Ember's code also keeps the text a file held
  before the agent overwrote, edited or deleted it (its newest 5), and `restore`
  brings it back: live, an overwrite left a book's 24 KB interior as its
  671-byte header. Since 0.40.3 that includes a text a draft overwrote or a
  workshop run's file of the same name replaced.
- **Listing photos** (`make_image`): up to three pages, sheets or pictures (or a
  zoomed-in region of one), fanned out next to a title, a subtitle and a badge,
  in Etsy's 4:3 size (3000 x 2250) or square or portrait; or a photo of words
  alone (what is included), or a simple typographic poster at print size; or
  (layout **table**, 0.38.0) a cover with a small table: the title, subtitle and
  badge on a band of the accent, and under it the agent's table (2 to 5
  columns, up to 8 rows under its header) full width, with a header row, rows
  in turn white and tinted, and grid lines. A row named Summe, Gesamt or Total
  is drawn on a deeper tint, and Ember's code adds up the numbers shown above
  it: a sum that differs is named in the agent's Check line (it can't know
  where the numbers come from; the agent copies them from its file). Up to 10
  a cycle, a whole listing's photos. The table cover is built in from upgrade
  request #10 (the workshop script that drew the Nebenkostenabrechnung's v9 and
  v10 covers): a fix of a cover's words or numbers no longer needs a workshop
  run against the daily cap.
- **Print files** (`resize_image`): one of the agent's pictures at exact pixels
  (a Printify print area, e.g. 3508 x 4961 for A3 at 300 dpi): the centre of
  the picture in the size's proportions, nothing stretched, resized and noted
  as 300 dpi. A second or third size of a poster costs nothing instead of a
  workshop run; the agent hears what was cut off and when a picture is drawn so
  much larger that it may print soft. Up to 6 a cycle. Built in from upgrade
  request #4 (the workshop's resize script).
- **KDP books** (0.25.0, with Amazon KDP on): a document's page can be one of
  KDP's trim sizes (`page: 6x9`), and such a book's interior can have up to
  160 pages. When the agent proposes a book, Ember's code makes its cover from
  the agent's front picture: an ebook's JPEG, or a paperback's full cover as a
  PDF (back, spine and front with bleed, as wide as its interior's pages make
  the spine). See [Amazon KDP](#amazon-kdp).
- **Cost statements** (`make_cost_statement`, 0.20.0): a Nebenkostenabrechnung
  from a JSON description of the tenants (Wohnfläche, Personen,
  Vorauszahlungen; since 0.23.0 the days of a tenant who moved in or out during
  the period), the costs with their Umlageschlüssel (Wohnfläche, Personen,
  Einheiten, or since 0.23.0 Direkt: each tenant's amount as the Messdienst's
  Heizkostenabrechnung gives it, on the sheet Einzelbeträge) and, when the
  building has more units than those listed, its whole Wohnfläche, Personen and
  Einheiten. Ember's code makes the Excel file
  (each tenant's shares, costs and Saldo; each tenant's part of each cost, to
  the cent; a statement to print for the tenant chosen in a dropdown), with
  formulas doing every sum so buyers can use their own numbers, and its cover
  picture: the tenants' table as German Excel shows it, 3000 x 2250. It works
  out every formula of the file itself and keeps nothing unless each number
  equals its own exact sums, so the cover always shows the file's numbers.
  It reads no meters (heating and hot water under the Heizkostenverordnung come
  in as the Messdienst's amounts), and it doesn't judge which costs a lease lets
  a landlord pass on. Up to 3 a cycle. Built in from upgrade request #7
  (the workshop's Nebenkostenabrechnung script; #8 asked for it again). Each
  variant, a commission with a client's own numbers too, is one more spec.

Pictures of a sheet (in `make_image` and the spreadsheets' own pictures) round
as Excel does since 0.20.0, and a German workbook's (a cost statement's) are in
German notation, 1.234,56 € and 31,97%. Since 0.37.5 both are worked out from
the file by the same code, a number in the General format is shown as Excel
shows it (1234567, not 1.23457e+06), and a date in its column's format, also
when a formula works it out.

The agent reads the text of its PDF, Word and Excel files and copies files
itself, for free (the workshop was paid for this). Pictures may have up to
40 megapixels, enough for a print-size poster; a PDF page is never drawn
larger than that either (0.21.0).

A long text (a guide, a planner's pages) no longer goes through the agent's
replies 2,500 characters at a time: with **draft** the worker model writes the
whole file in one call of its own (up to about 24,000 characters, a few cents
to a dime on claude-sonnet-5), from the agent's brief and the workspace files it
names; the file is saved in the workspace and never read back through the
conversation. Drafts count toward the cycle cap like research, at most 3 a
cycle, in ordinary cycles only. An append that the file's 64 KB might not hold
is refused before it is paid for; a paid draft that still doesn't fit is saved
as a new file next to it.

The agent can **look** at a picture (a page or a listing photo) before it shows
you anything, and it reads a short guide for each tool. Since 0.39.2 it hears
when the picture was made, and whether that was before the version of Ember
that runs now: the first cycle under 0.39.1 judged the new renderer by pictures
the old one had made, and rebuilt the book on that. The PDFs embed their
fonts (Carlito, Caladea and Poppins, all under the SIL Open Font License); the
Word copy asks for Calibri, Cambria and Poppins, which the first two replace
exactly, so it looks the same in Word. Where Poppins isn't installed, Word shows
another font. Files are limited to 15 MB each and 2 GB together, on top of the
50 MB for the agent's text files; the workspace holds up to 5,000 files (in up
to 1,000 folders). The agent's STATUS shows how much of that it uses.

Check a product before you sell it. The agent is told to disclose on every
product and listing that Ember, an AI agent, made it, which marketplaces such as
Etsy require.

**Who made it** (0.45.0, the evaluation of 2026-10-22). Ember's code adds a line
to every listing, pin, Printify product and KDP book that names the maker:
"Made by Ember, an AI agent, and checked by the seller before listing." (until
then "designed with the help of AI and reviewed by the seller", the opposite of
what happened). And every request the agent makes is refused before it reaches
you when what it would publish says that a person made it ("designed by a
human", "by the shop owner", "by the author", "by a solo creator", "von einem
Menschen gestaltet"), promises legal certainty ("rechtssicher", "GoBD-konform",
"§ 556 BGB konform", "legally compliant") or says "no refunds" ("keine
Rückerstattung"): in the week of 10-12, 13 of its 39 requests did, and you
approved them all. Refused, not flagged for you: your approval is the last
check, these claims have no true form the agent could check itself, and a
refusal costs it one rewrite in the same cycle. A format norm ("DIN
5008-konform") passes, as does a request that corrects such a text (the old
words a change replaces, or quotes in a request you carry out, aren't its own).

## The workshop

The workshop is how Ember gets things done that its own tools can't, and how it
grows new abilities.

**What happens.** The agent describes what it needs (for example "a bar chart of
these prices, chart.png, 1200 x 800 pixels") and can hand over up to five of its
files (10 MB together). A separate call to the workshop model, with Anthropic's
[code execution tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/code-execution-tool),
writes a Python script and runs it in a container on Anthropic's servers
(Python with pandas, matplotlib, Pillow, ReportLab, python-pptx, openpyxl and
more; no internet). Nothing runs on your Home Assistant.

**What is kept.** Ember's code downloads the files the run made and checks each
one before it goes into the agent's workspace (by default `workshop/out`):

- Pictures (PNG, JPEG) are decoded and saved again, so only their pixels are
  kept, not their metadata (Anthropic's Content Credentials included).
- PDFs are refused if anything in them can act on its own: JavaScript, launch
  or submit actions, embedded files, rich media, links that open other files,
  or an action when the file opens other than showing a page. Only web and
  mail links may point outside the file. Ember decodes the compressed parts to
  search them, so a PDF whose parts it can't decode, or an encrypted one, is
  refused too. So is one with a page no printer or screen shows: smaller than a
  quarter inch or larger than 200 inches a side, or more than 20 times as long
  as it is wide (0.21.0: one photo of a page 1000 x 10 points took 4 GB).
- Word, Excel and PowerPoint files are refused if they hold macros, ActiveX or
  embedded objects, links to other files or templates, DDE (also split over
  several runs), data connections or web queries, formulas or names that reach
  outside the workbook, or actions that start programs. Only web and mail links
  may point outside the file, and every part must be of a kind known to be
  safe (the format's own XML, PNG, JPEG or GIF pictures).
- Text files must be UTF-8, at most 64 KB. Anything else (SVG, archives,
  programs, fonts), and any file that can't be read whole, is refused.

Since 0.40.1 the agent's listing of one folder shows its subfolders, then its
files the newest first with the time each was written, 40 at most ("… 151
older"): listed by name, the first 100 of `books/` (185 files, versions v3 to
v22 of the Haushaltsbuch) took 4 KB a listing, and four listings in one cycle
still didn't say which file was the newest. The whole workspace is listed by
path, as before.

Since 0.39.2 the workshop's result says first where it kept a file whose task
named another folder for it, and wherever a file the agent asks for doesn't
exist, the refusal names a file of that name elsewhere in its workspace: the
book's v14 was kept in `workshop/out/` and asked for in `books/`.

The run's script is kept in `workshop/scripts/`, so the agent can run it again
instead of paying for it to be written anew. Since 0.24.0 a script run again
without files gets the files its first run had (a script that read a workbook
ran without it and made up its numbers), and a task that names a file of the
workspace it doesn't hand over is refused before it is paid for. The files
handed over are uploaded for the run only (they expire within the hour), and
every file is deleted from Anthropic's storage when the run is over. A run that
keeps none of the files its task names says so, and points the agent to its
answer for why (until 0.35.2 it told the agent to name its files, though it
had: live, six runs failed in a row while it reworded them); the diagnostics
report shows each run's answer.

**What it costs.** A run is one metered call (up to three if a long run
pauses): tokens at the workshop model's prices, plus sandbox time. Anthropic's
first 1,550 sandbox hours a month are free, then $0.05 an hour; Ember books at
least 5 minutes per call anyway, the most it could cost. Live, runs cost
$0.02–0.35, and one cost $1.84. Ember prices a call as up to 10 rounds of the
model, each writing up to 3,000 tokens and reading what the rounds before it
added (about $0.60 with claude-sonnet-5), and refuses a run priced above what
is left of its **cap per run**. That price is not a ceiling: nothing limits
what the run's code prints or looks at, and that one run read ten times more
than the price allows for. So each call keeps back the most of its cap per
run, its raised estimate, and 1.5 times the costliest of the last 20 workshop
calls in 14 days (what they are known to cost, interrupted ones included),
under the daily cap and the balance. Since 0.33.0 a run keeps back no more than
the day has left, and never less than its own worst case: live, a $2.76 hold
refused every run once the day's spending passed about $3, though each run was
priced below what was left. The balance still counts all of it: a run needs 5
times what it would keep back without the day's limit left above the last
will's reserve (see [Money](#money)). Until 0.37.2 the day's limit included a
fifth of the balance, so near the bottom a run kept back that fifth and always
passed: one that cost $3.21 holding $0.62 would have ended Ember below zero
without its last will. **Reset
estimates** doesn't clear those runs; they stop counting after 14 days. A call
can still cost more than all of this; Ember then books what it cost, makes no
more workshop calls in that cycle, and raises the workshop's estimates. Runs
count toward the daily cap and the balance but not the cycle cap (one run can
cost more than a whole cycle may), and **runs per day** limits how often the
agent uses it. Until 20:00 they leave the event wake-ups' share of the daily
cap, and there are no runs in the maintenance burn mode. Switch it off with **Workshop** or 0 runs per day.

**How Ember grows.** A script proves itself when the agent runs it again, or
when its files go into a request you approved. The planner then reminds the
agent, and it files an **upgrade request** with the script attached. On the
dashboard, that request shows the script with **Download script** and **Copy as
a task for Claude Code**: the copied text holds the request and the script, ready
to paste into [Claude Code](https://claude.com/claude-code) (or to give to any
developer) to build it into Ember as a proper tool. Built in, it runs on Ember's
own code, costs nothing and can't break the way a newly written script can.
Once it is in a version, mark the request **released** with that version and
name the tool in the note: the agent hears it at its next wake-up.

Since 0.22.2 a script is asked for once: the agent can't file a request with a
script (or the same code under another name) that an earlier request carried,
and the planner no longer reminds it of scripts kept by running that script
again. After you mark the request released, the workshop refuses to run the
script again, since its tool does the same free. (Upgrade request #8 asked
again for the Nebenkostenabrechnung script that 0.20.0 had built in from
request #7 as `make_cost_statement`.)

In dry run the fake model pretends to run code: it "draws" a simple chart and
returns it with a script, and nothing is sent to Anthropic.

## Ventures

Ventures are the agent's ways to earn beyond what it does now: a new market,
platform or business model, or a channel that brings buyers to what it already
sells (a Pinterest account for the Etsy shop is a venture of its own). Since
0.37.0 they are part of your [plan](#plan), and the **Plan** tab shows them
below the milestones (until then they had a **Ventures** tab of their own): a
tree that keeps growing, and the projects that do their work (0.27.0: the
Projects tab is part of it). It has two views, and this browser keeps the one
you chose last:

- **Ventures** (**Pipeline** until 0.37.0): the tree, each venture's step in
  your plan (the decision desk until 0.37.0), and the ventures still being
  decided: business cases for you, ventures being researched and ideas, then
  the parked and killed ones. Its badge counts the business cases waiting for
  you, as the Plan tab's does.
- **Product lines** (**Running** until 0.37.0): the ventures you backed
  (building) and the live ones, each card with the projects that do its work
  inside it, then the other projects (of no venture, or of a venture that isn't
  backed or live). A venture you back moves here with its card; one you park
  or kill moves back to Ventures. When the card still has your focus, the view
  goes with it.

**The tree.** Every idea branches from the one it grew out of: a variant, a
niche, another customer group, a channel, a next step research turned up. The
tree started with the ideas you and the agent had before 0.10.0 (the Etsy leg,
Pinterest, dropshipping, print on demand, a website with ads, recruiting, an AI
chat companion, and Fiverr, parked). Nothing is ever deleted: parked and killed
ideas stay in the tree, dimmed, so they aren't started again. Click a node to
see its card.

**The weights.** The agent scores every venture from 1 to 5 on possible
revenue, doability (how much of it the agent can do, and how little of your
time it needs), difficulty, risk, speed to the first euro, and the cost to
start. The weight (0–100, revenue counting double) is the node's size; its
colour is its stage. A brainstorm's scores are a first guess (dashed); research
replaces them. The agent's own scores count only after research: Ember's code
records each research call made for a venture (in a venture cycle, the one it
focuses on), and the agent can score a venture once one of them found web pages
(in a venture cycle, where it also writes the business case).
The card's **Research** line counts them.

**Venture cycles.** Since 0.36.0 your [plan](#plan) decides when the agent
explores. Since 0.37.0 each venture being explored (an idea, a venture being
researched, a business case) is a node of the plan's **Ventures** project, and
its next decision (below) a step of it, weighed like any other step: worth what
its business case expects a month (the critic's number where it is lower), as a
product's worth from what it could earn, at least 2 (a product that could earn
$5 a month); an idea's from its scores, 1 to 3 (an unscored one 2), so the
heaviest ideas are researched first; or the worth you set. It is more urgent
when you asked for the venture's research or added the idea, or when Ember's
code parks the venture within a week by its stage's rule, and it weighs more
the longer it has waited. When a venture's step weighs most, the cycle is a
venture cycle on that venture. (In 0.36.0 one **Explore** step stood for all of
them, and a venture cycle's plan then took one venture from the decision desk.)
While no step of a product is ready (a new install's first cycles), exploring
and starting a new product take turns: a cycle that may start one comes between
two venture cycles. On the Plan tab's **Ventures** box you set the worth of all
your ventures (empty: each its own), and **Hold new things** for "nothing new"
(no venture cycle and no new product until you **Resume**). **Explore next** on
a venture's step (the box's **Their steps** leads to them) pins it: the next
venture cycle takes it, even when it is a new product's turn, and the pin is
spent. Until 0.35.3 the **Share for ventures** option made a cycle
a venture cycle while venture cycles had had less than that share of the day's
spending: live, 10 of 24 cycles were venture cycles the agent, told "nothing
new", left undone, and cheap idle cycles made the share want more of them. The
option is gone; an old setting is ignored (if yours was 0, set the ventures'
worth to 0.5 on the Plan tab, or **Hold new things**, which stops new products
too). Venture cycles are for new ventures: ideas, research
and business cases (0.19.3). A venture you back becomes project work: Ember's
code opens its project, and ordinary cycles run its first test. In a venture
cycle the agent:

- works on its step, **YOUR STEP** (since 0.37.0; until then one of the
  decision desk's items it chose): its venture's next decision, what it needs
  now, its next question and what done means. Ember's code aims the cycle at
  that venture;
- when its step is a **brainstorm** (a step of the Ventures project while the
  ideas run low, below), grows the tree: a separate call on the planner model
  that finds six new ideas that fit you and the agent (about $0.05–0.15 with
  claude-opus-5-5), branching from a promising venture or into new ground;
- researches one venture (up to 8 web searches instead of 3), keeps what
  it learns in the venture's knowledge file (`ventures/<number>-<name>.md` in
  its workspace, which the card opens; a full one continues in
  `…-2.md`, `…-3.md`) and scores it again. The card's **Files** opens every
  file written for the venture or its projects on the Workspace tab (0.26.0).
  In a venture cycle every research call is a venture's (the one it focuses
  on, unless it names another); in
  another cycle, one on a product line of a venture that isn't backed counts
  for that venture too (0.28.0; since 0.15.0 it counted for a venture the plan
  focused on), and a paid call that failed counts toward its budget. A
  question it asked in the last 30 days that found web pages (the same words,
  site or page) is answered from that research, free, and doesn't count as
  research for a venture;
- decides each venture within its **research budget**: $0.60 of research
  calls while it isn't backed. Once that is spent, Ember's code refuses more
  research for it, and the agent brings you a business case or parks it with
  the reason. The card shows what is left. **Research next** (**more**,
  **again**) gives a venture a new budget: only you can.

**Each venture's next decision.** Ember's code names what each venture needs
decided next (0.13.0), and since 0.37.0 it is that venture's step in your plan:

- **triage** an idea: research it or park it with why, while fewer than 8
  ventures are being researched or proposed (backed ones don't count; until
  then the step waits for room). Since 0.39.1 the step names the other ideas
  waiting for theirs, so one cycle triages them together: on 10-14 six cycles in
  a row each parked one idea of one brainstorm ($1.03). Since 0.45.0 the ideas
  it didn't take along wait for the next day once a cycle of the day took a
  triage, so one cycle a day decides them (not your own ideas, a step you
  pinned, or an idea Ember's code parks within 7 days);
- **appraise** a venture being researched: what it needs next (research,
  evidence, numbers, a knock-out to fix), then its business case, or park it;
- **answer** the critic on a proposed venture it says to test or park: with
  evidence or new numbers, or park it;
- **your decision** on a business case: back it or park it (the step waits on
  you);
- a **brainstorm**, a step of the Ventures project of its own while fewer than
  5 ideas wait and fewer than 5 ventures have their numbers; since 0.39.1 not
  within 7 days of the last one while none of its ideas was researched (the six
  ideas parked in a day left the funnel empty, and the next brainstorm came at
  once: $1.76 in two days, nothing researched); since 0.45.0 not while a rule in
  your rulebook or the agent's strategy says nothing new starts ("nothing new",
  "no new ventures", "keine neuen Ideen"): your rule of 10-07 stood until 10-15,
  and two brainstorms made 12 ideas in it.

A venture's step can wait for a day (since 0.39.1, `plan_step` wait in a venture
cycle, at most 14 days ahead): an idea the agent means to take up after your
"nothing new" week waits until then. Until then nothing could put one off, and
an idea "paused until 10-20" came back the next day.

A venture you backed has no next decision here (before 0.19.3 it had **build**):
its project is ordinary cycles' work. Once a venture's stage moves on, its step
is done and the next one takes its place; a venture you back, or one parked or
killed, closes its node.

Until 0.36.0 these were the **decision desk**'s **READY** list, ranked by rules
of its own (your wishes, ventures close to being parked, answers to the critic,
the other appraisals, triage, a brainstorm last; within each, the highest
expected net), and each venture cycle's plan took one item or said why it took
none. Since 0.37.0 the plan's weights rank them, among all the other steps:
your wish and a park soon make one urgent, its business case's expected net is
its worth. Venture cycles run only in the explore burn mode. Under **Their
steps in your plan**, above the tree, each venture's step shows with its
weight or what it waits on and what it needs now; the summary above them says
how many ventures were decided in the last 7 days (proposed, parked or killed
by the agent or you, not parked by a stage's rule; the aim is 2 a week) and the
record of the agent's forecasts. Venture cycles work within the daily cap; they
have no cap of their own.

A venture cycle only researches and decides: its work steps don't carry the
tools for making files, the workshop, the Etsy shop, email, Reddit or changing
the plan's steps (they belong to ordinary and marketing cycles). With the shorter rules of 0.12.0, which no longer
repeat what Ember's code enforces or what the constitution, your knowledge
file or a tool's description already says, a venture cycle's fixed prompt is
about 30% shorter than in 0.11.

**Numbers.** A business case comes with numbers (0.13.0): the agent gives
the price, the cost per sale, the fixed costs a month, its low, likely and high
estimate of sales a month (P10, P50, P90), the cash to start, your hours a
month, the days to the first sale (0.15.0: at least 14) and its own API spend
on it. Ember's code
works out the rest the same way for every venture: the fees (Etsy's for
Germany: the listing fee again at each sale, 6.5% of the sale, payment
processing of 4% and €0.30, and VAT on Etsy's fees), what a sale keeps, the
sales a month that break even, the net a month at each estimate, and the
expected net a month over six months per API dollar and per hour of yours
(0.15.0: the months before the first sale earn nothing and still pay the fixed
costs).
The card shows the newest case; a venture is proposed only with one. Without
your exchange rate (**Exchange rate for Etsy revenue**), USD 1.10 per EUR is
assumed and said.

**Knock-outs.** Before a venture is proposed, Ember's code checks its case
against six knock-outs:
- cold outreach (writing to people who didn't ask first, which is illegal
  advertising in Germany), found in the case's words or declared by the agent;
- accounts Ember itself would have to create;
- more cash to start than **Cash for a venture's first test**;
- a first sale later than half the net runway (in days; the first sale counts
  as at least 14 days, so below 28 days of net runway every case is slow until
  you lift it). Since 0.42.6, with the spending stance **invest** (the
  default), later than the whole net runway: invest explores whatever the
  runway, and with 24 days of it nine venture cycles ($2.48) found every case
  slow and parked it, so none reached you;
- a sale that loses money after the fees;
- no independent page behind its demand: no claim of searches, sales, orders,
  reviews or buyers from a page that isn't a vendor's or an affiliate's (0.15.0:
  a price or a policy doesn't count).

A knocked-out venture isn't proposed: the agent fixes what can be fixed (new
numbers, independent evidence) or parks it with the numbers. Since 0.32.0 a
refused proposal names everything it still needs at once, the knock-outs that
stand too, and keeps the rest of the agent's update (its case, its notes): it
threw them away, and the agent sent them again and again. The card lists
its knock-outs, and you can **Lift** one for that venture if you accept it
(and **Restore** it later); the agent hears it as your note on the venture.
A venture Ember's code wouldn't back (no numbers, as one proposed before
0.13.0, or a knock-out that stands) says why on its card (0.15.0), and **Back
it** becomes **Back it anyway**: your call. A proposal that turned out so
since the card loaded goes back to researching instead, and you see why.

**Critic.** The agent argues its own cases, so before its next plan after a
venture is proposed, a separate call on the **Model for venture plans and
reviews** reviews the newest case (0.13.0): the pitch, the case, the agent's
numbers with Ember's code's economics and the evidence by grade. It answers
with the fatal flaw, its own numbers for the same case, a verdict (**Back it**,
**Test first**: only a cheaper first test, or **Park it**) and what would
change its mind. Ember's code checks the answer and works out the economics of
its numbers the same way (with the agent's cash to start, your hours and the
API spend). The card shows the critique beside the case, the agent sees it in
its focus, and a venture ranks by the lower of the two expected nets. It costs
one call per case (about 1-3 cents on Opus), counts toward the daily cap but
not the cycle's, and runs only while venture cycles do (not in the maintenance
or dormant burn modes). A critique that fails is tried again at the next cycle,
twice in all; a new case gets a critique of its own.

**Evidence.** In a venture cycle the agent saves each number its research
finds (a price, searches a month, sales, a fee, a margin) as evidence: the
claim, the metric, a low and a high value, the unit, the region and the page it
is on. Ember's code grades the page, and neither the claim nor its grade ever
changes:

- **independent**: a page one of the agent's research calls returned (Ember's
  code keeps each call's pages, matched without tracking parameters);
- **marketing**: a vendor's page (a business selling what the page describes:
  Shopify, Printful, Printify, dropshipping suppliers such as BigBuy, Etsy
  research tools such as eRank or Marmalead, course platforms and the like, on
  any of their domains, shopify.de too) or an affiliate's link (one that pays
  whoever sends a buyer), even when research returned it;
- **unchecked**: a page no research call returned: the agent's word only.

A venture's card lists its evidence (**Evidence: … claims**), each claim with
its grade and a link to its page, and the agent's plans show it by grade with
the newest values.

**Business cases.** A venture the agent proposes comes to you with its demand
(evidence that people pay), economics, setup (money, your hours, accounts,
new abilities it needs), how soon the first euro could come, the risks and legal
duties, and the smallest first test. Ember's code (and its database) lets the
agent propose one only after it was researched: from the researching stage,
with at least two research calls for it that found web pages, all six scores
from research and a business case that names a source link or an amount in
euros. The tab's badge counts the ones waiting. On each card:

- **Back it**: the agent builds it: its first test becomes a milestone on the
  roadmap (**First test: …**, set by Ember's code, due in 21 days, with the
  business case's first test as its measure), Ember's code opens its project
  (0.19.3; ordinary cycles work on it), and the agent asks you for what only you
  can do (accounts, money, setup), one step at a time. A venture of a channel
  Ember's code serves (Pinterest, Printify) gets its first test only once that
  channel is set up, so its 21 days don't run out during your setup (a first
  test set while the channel wasn't set up is dropped, and a new one starts
  once it is). The venture goes live only once that first test is met as
  Ember's code checks it, or you drop it on the Plan tab: the agent's own
  word doesn't count (for a first test without a metric, it tells you, and
  your drop confirms it).
- **Research next** (or **Research more**, **Research again**): it goes first
  in the next venture cycle.
- **Park**, **Kill** or **Note**, each with an optional comment (a note needs
  one). The agent reads your word on its next wake. A venture you park stays
  parked until you take it up again (**Research again** or **Back it**): the
  agent can't, and its card says **Parked by you**. The agent can take up the
  ventures it parked itself. 0.22.0: your park or kill stops the venture's
  projects too: a kill closes them, a park makes them wait (the agent can't
  make them active again until you take the venture up), and the agent opens
  no project in a parked venture and moves none into one. 0.23.1: nor out of
  one you parked or killed. A park keeps each project's state in its
  notes, and taking the venture up again gives it back. 0.23.2: it also
  drops the open milestones of the venture's projects (the agent's too), and
  the agent can't focus a plan on a waiting project, link a milestone to the
  venture or its projects, or add listings and products to them. 0.23.3: nor
  change, pin, post or blog about, or bet on their listings (it can still ask
  to deactivate one), nor research the venture; what you approved for them
  that hadn't run yet isn't carried out (your own Undo still is). Parking the
  Etsy leg or the print-on-demand venture stops that channel's work for its
  lines and for lines of no venture (parking print on demand stops the Etsy
  leg's Printify products); a venture you backed sells on.

**Each stage's rule, kept by Ember's code.** Before every plan, Ember's code
parks a venture whose research brings no business case within 21 days of its
first research call (while nothing is built for it: no open project), and a
backed venture whose first test is missed (closed missed, or still open a week
after its date: a first test Ember's code checks has that week too, and is
met if its number comes in it). A first test of Pinterest or Printify that is
still unmet then while no pin or product was ever made never ran: it starts
once more instead (once). A week before a first test's date, the System log
tells you once what is at stake (when it is parked), and the agent's plan lists
it among what it owes until a message of the agent's names it (since 0.39.1:
then it says the agent told you, and to work toward it; on 10-14 nine cycles
from 04:20 on tried to tell you again what two messages had). It also parks an
idea of the agent's that no one took up (researched) within 30 days, the
**triage** (your own ideas wait for you), and a
**live** venture that has sold nothing 60 days after it went live (no revenue
recorded for it, no Etsy order of its listings). A live venture that earns more
than it costs gets a milestone instead: **Scale it** (21 days), a goal of its
own under your goal, which the agent closes once more of what sells is under
way. Ventures already waiting as ideas or live when this version came count
from the upgrade, so none is parked at once. A venture parked this way says **Parked by Ember's code**, with
the reason in its notes; like one you parked, only you take it up again. Only
you back or kill a venture: the database refuses it from anyone else. A venture
that is parked or killed takes its open milestones with it (when the agent or
Ember's code parked it, yours stay yours to drop). While you park or kill it,
its products' steps wait and their decide-by dates aren't read. Each card shows
its stage's rule, and so does the agent's plan.

**Add idea** puts your own idea into the tree, optionally as a branch of an
existing venture: the agent scores and researches it and tells you what it
would take. The agent answers an idea with the path, the smallest test, the
numbers and its recommendation. A no is a result when data backs it: it comes
with the numbers and the closest test, and the venture is parked with them. Its
hard rules (spam, gambling, adult content and the like) are a no without a
test, and then it offers the closest variant that keeps them. It researches
before it builds: a research call costs about 5 cents, a product with its
listing many times that.

**Money.** A venture's cost is what the model calls whose work served it
cost: the work of the cycles aimed at it or its projects, and the research
calls made for it (plans, reviews and brainstorms are overhead, counted for no
venture). Each card shows its P&L: what it **earned** (the revenue recorded
for it or its projects, less refunds), its **expenses** (Etsy's fees, say), what
it **spent** and the **net**. The agent's plans and daily review show the same
numbers. Live legs show spent and earned in the tab's summary.

## Marketing cycles

Since 0.35.0 a cycle is a marketing cycle when its [plan](#plan) step is a
marketing step: a pin, a Bluesky post, a blog post, or a request that a
marketing cycle makes. (0.28.0 to 0.34.0 the **Share for marketing** option gave
marketing cycles a part of each day's spending; that option is gone.) A
product's marketing steps come from its type's launch stage and, once it is
launched (its first marketing out; since 0.39.3 whether or not the critic has
passed it), the recurring steps of its maintain stage: a pin a week, a Bluesky
post a week for English products, a blog post a month for German ones. A missed
day-7 views date makes its marketing urgent for a week.

A marketing cycle brings buyers to its step's product: pins and Bluesky posts
that link its listings, a blog post that recommends one, the link page, a
Reddit draft you post, better titles, tags and photos (a change you approve).
Ember's code keeps every link on that product's listings. The agent bets on
what the reach will bring, so the next marketing cycle learns from it. A
marketing cycle makes no documents, spreadsheets, new listings, products or
books and sends no email. It can read one, though (since 0.45.1): `email_read`
opens an email by its number, so asked by you about an Amazon KDP, Etsy or
Printify notice it can say what the notice asks; answering stays an ordinary
cycle's. Demand notes, which a line's first listing needs, are an ordinary
cycle's too.

An ordinary cycle doesn't carry the marketing tools, and its plan doesn't show
Pinterest, Bluesky or the blog, with three exceptions: a channel that waits for
your setup still says so (so Ember doesn't ask you again), an ordinary cycle on
a channel's own product (setting it up) keeps that channel, and (since 0.37.7)
Bluesky's numbers stay: the plan keeps one line with the account's followers
and the live posts' reactions, and `bluesky_posts` reads each post's, so Ember
can tell you how a post did (proposing posts stays a marketing cycle's). The
**Activity** list marks each cycle with its kind and its line (*Marketing · #4
Nebenkosten*, *Product line · #7 …*).

## Plan

**Since 0.35.0** one **plan tree** under your goal decides what Ember works on:
before each cycle Ember's code takes the cycle's step from it. (In 0.34.0 it
ran in the shadow, beside READY.) The **Plan** tab shows it, with your goal at
the top, your milestones below and (since 0.37.0) your ventures last. It
replaces the Roadmap tab and (0.37.0) the Ventures tab.

**Your goal** (0.29.0). **Set your goal** asks for an amount in USD (the books'
currency), whether it counts **a month** (revenue less expenses over the last 30
days reaches it: a steady income) or **in total** (everything from the day you
set it adds up to it), and the date to reach it by, at least a week and at most
a year ahead, with an optional word on why it matters, which the agent reads with
it. Ember's code checks it from the revenue and expenses you record in the
Ledger, before every plan and whenever you open the Plan tab: met, it closes it
**done**; past its date, **missed**. Only you change it: **Change goal** sets a new
one in its place (what led to the old one leads to the new one, and a goal in
total you raise keeps counting from the old one's day), and **Remove goal** takes
it off (what led to it stays). The agent can't move, close or re-link it; it may
only leave a note on it.

**Ember's own goal.** While you have set none (and after yours is met, missed or
removed, until you set the next), Ember's code keeps a money goal of its own at
the root, **Earn as much as you spend** (over the last 30 days, the revenue
recorded less expenses at least equals the API spending), due in 90 days, so the
agent always has a goal. It checks it from the books before every plan: once it
is met it closes it **done** and sets the next, which asks for more (twice, then
three times what the agent spends); past its date it closes it **missed** and
sets it again. The moment you set your goal, it gives way to yours: it is closed
(dropped by Ember's code), and what led to it leads to your goal. You can drop
it: then Ember's code sets no more of its own, and only your goal leads the
plan.

**The tree.** Under your goal are the projects, one for each platform (Etsy,
KDP, Printify, the website, the channels), and under each project its products:
every open product line is one, laid out the first time Ember's code sees it,
from its type's template (an Etsy download, a Printify product, a KDP book,
website content, a channel). A line whose words name no type is laid out as a
plain product; once its records name one (its first listing request, a
Printify product, a KDP package), it takes that type's stages and steps. Since
0.40.2 a line with an Etsy listing (its own, or one moved to it) is no KDP book
by the tools its cycles used: a check of the Haushaltsbuch in a cycle of line
#12, the coach bundle, made it a KDP book on 10-19. Such a line takes a plain
product's stages again, with what Ember, you or her code added there. A
product has stages (research, create, release, launch, maintain), and each stage
has small steps. A step or a stage counts as done only when its check passes in
Ember's records: a demand note, a request to you, a live listing, pins, Bluesky
posts and blog posts that link the product. A later stage done closes the ones
before it. Once a product is launched, its maintain stage gets a recurring step
for each channel its audience uses, and a fix whenever the quality check says
improve (the step quotes what the critic said). It is launched when its stages
before maintain are done, and since 0.39.3 its launch counts once its first
marketing is out: the critic's pass (maintain's fixes take its suggestions
over) and a step waiting on a channel that is off no longer hold its weekly
steps back, and once launched it stays so. Live, the critic passed no listing in
13 checks, so after their first pins and posts the live products had no
marketing step at all, and a missed day-7 date had nothing to put first. A
channel's own line (the Pinterest test) gets none since 0.39.1: a pin or post
links a line's own listings, and it has none (the tools refuse another line's),
so its steps for them dropped; its products' marketing is theirs. A
product's audience (English, German or both) comes from its words: since 0.37.8
a German market (Germany, Deutsch, a lone "DE") makes it German and only an
English one English, and when the words change, Ember's code drops its steps
for a channel that doesn't reach the audience (an English Bluesky post for a
German product) and adds its template's for one that does, saying why in the
day's changes. The blog counts as a channel by **Blog** alone (until 0.37.8
the Plan tab also asked for **Website**, and took the blog for off while
Ember's cycles wrote its posts). The Plan tab names each step's product, as
every product's steps have the same titles. A promise
Ember makes you about a product becomes a step of that product, and so does your
decision on one of its requests. Since 0.37.0 the tree also has the **Ventures**
project, where each venture being explored is a node and its next decision a
step (see [Ventures](#ventures)), and the **Owner** project, where a promise or
your decision about no product is a step. A promise names its product with message_owner's
project_id, or (0.35.1) by its words: the number of one of Ember's listings, or a
type with only one product open (KDP, Printify). A promise of pins, a Bluesky post
or a blog post is a marketing cycle's step (since 0.37.6 an Owner promise too: it
was an ordinary cycle, which couldn't make a pin; a promise to report on a channel
is told in a message, and stays an ordinary cycle's). A promise whose product has a request
waiting for you, made since the promise, waits for your decision.

**A request proposed again** (0.37.6). A stage that still needs a request to you
(the listing, the product, the book) gets a step of Ember's code's to propose it
once none of that kind waits for you, is approved or carried out, and the stage
has no other step of Ember's open: **Propose the listing again: request #12
expired** (or withdrawn, rejected, failed), done when a new request is made.
Until then the step that proposed it had closed on the first request, a closed
step stays closed, and the product never had a step ready again, while "You
approve it, and it goes live" said it waited on you with nothing waiting. That
step of yours now waits on Ember's proposal while no request waits for you. The
same step proposes a product's first listing once its files and photos are made
(the create stage needs it proposed).

**How a cycle gets its step.** A step you pinned; else the heaviest step. Since
0.37.0 a promise to you and your decision are weighed like any step, and so is
each venture's next decision (since 0.36.0 the ventures' turn, a share of the
day's spending, is retired; their steps wait while the message you sent, which
woke the cycle, waits for an answer). Until 0.36.0 a promise to you or your
decision due within a day came first, and any other promise from the moment it
was made, the soonest due first, up to three times a day (your decision once).
To leave the product it is on, another step must weigh 25% more, for up to
three cycles in a row. The step decides the cycle: a marketing step makes a
[marketing cycle](#marketing-cycles), a venture's step a
[venture cycle](#ventures) on that venture, any other an ordinary one, and
Ember's code keeps the cycle's tools on its product line. With
no step ready, Ember starts a new product in the explore burn mode (unless you
hold new things, or since 0.37.6 steps of hers wait for tomorrow: the brake,
below); otherwise nothing new starts. While her plan has steps ready
that a cycle can advance, her code cuts any cycle's long sleep to your shortest
sleep (0.37.6: not for a step whose last try moved nothing).

**The brake** (0.37.6), one rule for every step. A step a cycle took without
anything it is checked by moving waits until something moves, or until the next
day, when it is ready for one more try (it cuts no sleep). What moves a step:
the numbers its check counts (pins and posts live, files and photos made, demand
notes, the critic's verdicts) and its own requests (one made, or decided by you,
expired or carried out). A promise, your decision and a step Ember says is done
count nothing of their own until they are kept: what moves them is their
obligation closing and what becomes of their product's requests (your decisions,
an expiry, a request carried out or failed), and for a promise or a step of
Ember's also what the cycle that took it made for its product (a file, a
picture, a demand note, a request). A venture's step moves with its venture's
stage, its evidence, business cases and the critic's answers (not with a note or
a rescore alone). Your pin of a step moves it too. A step whose own requests are
on their way, and all its check still needs (two pins asked for or live), waits
on them by itself: on you while one waits for your decision, then while they are
carried out, without one of Ember's two waits a day. A cycle that failed or was
refused doesn't count as a try. While steps of hers wait for tomorrow, no new
product starts (they are her plan's work still). Live on 2026-10-09 a product's
"Pin it twice", whose pins waited for you, took every cycle 30 minutes apart
until the daily cap stopped it: $5.88 of $7 by 12:35, and the other products got
none. The Plan tab says why a step waits ("taken today, and nothing it is
checked by moved"); pin it to have the next cycle try it again today.

Since 0.39.8 a request you **rejected** and Ember proposes again ("Propose the
book again: request #120 rejected") is urgent, and two cycles a day may take it
before the brake holds it. Your decision weighed a lot as something owed; once a
cycle had taken it in, the step proposing the book again weighed 2.6, below a
promise about a Bluesky post, and the book you called "finally looking good" got
no cycle for a day. Since 0.40.1 it stays urgent until Ember proposes it again
(or you hold or drop the product), not for 3 days after your decision: five
cycles in those days ran out of room before they proposed the book, and from the
fourth day it got one try a day, weighing as much as a step of the daily review.
Since 0.42.1, when your rejection means "not this", Ember may **decline** the
rework (`plan_step` decline, with why in a sentence): the step closes with her
reason on your Plan tab, and nothing proposes that request again; **Rework it
anyway** on that step brings it back, pinned. A later request you reject gets a
step again.
Since 0.40.3, when the line has no listing yet and its newest demand note is
older than 14 days (Ember's tool refuses a first listing then), a step for a new
note comes before the proposal, which waits behind it.

Since 0.39.8 a product's weekly pin, Bluesky post or monthly blog post waits
(*its channel brings next to nothing*) while that channel is worth less than
twice its floor, unless you pin the step; and Ember may **skip** one for its
period (`plan_step` skip, with why, which you see), outside its two waits a day:
the channel's next step for the line comes when the period ends. On 10-16 nine of
twelve cycles went to Bluesky posts and blog posts its own strategy had stopped,
as Ember could neither replace the code's steps nor put off more than two a day.
A product with no listing of its own gets no weekly pin or post, and one already
open waits (*it has no listing of its own*). Since 0.40.3 a new product's launch
posts and blog post on such a channel wait the same way (unless you pin them),
and its weekly pin on the channel that works comes while they wait.

**Your limits** (0.37.9). Two of your protections were enforced by Ember's tools
alone: at most 3 publish requests (pins, Bluesky posts, blog posts, the link
page) wait for your decision at once, and since 0.37.8 at most 3 of a channel's
pins (or posts) wait for you or their slot (since 0.39.6 approved ones of one
listing count once, and another of that listing waits until they are out: three
of the Haushaltsbuch's, slotted two days apart, kept every other listing's pins
out until 10-20); at most 2 of Ember's messages a day
answer none of yours. The plan took a pin, a post or a report as ready whatever
they said: the sleep was cut, a plan was paid for, and the tool refused the
request (in the analysis of 0.37.8, 16 of the 30 cycles #192 to #221, $4.12).
Now a step whose work is a pin, a post or a blog post (or a promise of them)
waits while its channel takes no more requests, until you decide one or one
goes out ("the publishing queue is full"), and a report to you (a promise to
report, or a step of Ember's of the kind report) waits once the day's two
messages are sent, until the next day or your next message ("today's messages
to you are sent"). A report waits only while the limit would refuse it: not
while a message of yours waits for the answer that can carry it, nor for a
promise Ember hasn't written to you about since making it (a message naming it
goes through). These waits take none of Ember's two a day, and such a step cuts
no sleep: Ember's code reads the limits again as a cycle ends, so a step the
cycle's own request or message put out of reach doesn't cut it either. Since
0.38.1 the same holds for the sales: at most 6 sell requests (a listing, a
change of one, a Printify product, a KDP book) wait for your decision at once,
and while they do, a step whose work is one waits too ("as many sell requests
as may wait for you wait"): "Propose the listing" (the product, the book),
proposing one again, and the critic's fixes, which are a change of the listing.
Since 0.40.3 a report also waits while five of Ember's messages are unread, when
she can't write to you at all ("5 of Ember's messages to you are unread"), until
you read them. Since 0.42.0 each of these rules is written once, and both the
tool and the plan read it; two more rules of Ember's tools are read the same way:
KDP's limit of 2 new titles of a format a week, and the demand note a line's
first listing needs. A step whose tool would refuse it for one of them waits
too, and cuts no sleep ("Ember's tool would refuse it now", with the tool's
words), until the next KDP week or a new note. The platforms' daily limits (a
day's listings, pins, posts, emails) aren't among them: Ember may propose more,
and what you approve goes out the next day.
The limits themselves stay as they are. The [diagnostics](#diagnostics) report
lists each step's wait with the limits as they stand ('queue', 'messages',
'sales'). Since 0.39.1 the agent's STATUS lists the day's messages that count
toward the limit, each with its number, time and first words ("2 of 2"), so it
sees the limit reached before it writes: on 10-14 nine cycles from 04:20 on
tried to send what two messages had told you hours before, each refused.

**How a step is weighed.** A product's worth is what it could earn a month (from
Ember's revenue sub-goal for its line, else its venture's split, else its type's
default), damped so that optimistic numbers count less ($5 a month is 2, $20 is
4.6, $60 is 7.4), times its chance: 0.4 in research, 0.6 in create, 0.8 in
release and 1 once it is live, and then, after 30 views, how it sells. A step's
weight is that worth times its kind (shipping, launching, fixing and marketing
1, creating 0.8, chores 0.5, reporting 0.3: since 0.37.9 a promise to report
too, which weighed as a promise to ship until then, so near its day a report
came before a live product's first pins) times its channel's results (since
0.35.3 only of the pins and posts live a week or more), times one plus its
urgency, its age (0.5 for each
day it has waited ready) and momentum (1 for the product worked on last). A
promise to you and your decision (0.37.0) are worth at least 3 (0.37.1; 5
before), whatever their product is worth, and urgent only near their day:
nothing until two days before it, 2 then, 3 the day before, 9 on the day and 11
once it has passed; once cycles took one three times in a day without keeping
it (your decision once) it has no urgency until that day is over. Until two days
before its day a promise weighs its worth and its waiting, at first less than a
live product's first pins: in 0.37.0 every promise weighed at least 15, more than any
step of a product, whenever its day was. A
venture's step (0.37.0) is a creating step, worth as [Ventures](#ventures) says,
urgent by 2 when you asked for it and by 1 when Ember's code parks the venture
within a week. The other urgencies: since 0.35.3 a defect the critic found (a
score of 3 or less) 5, a missed views date 4 on the product's marketing, a live
product's launch marketing 3 (its first buyers) and the critic's other
suggestions 1, so a live product's first pins come before its polish (until
0.35.2 every suggestion counted as a defect, and no pin was made). Since 0.39.1
the critic's other suggestions weigh as a chore, with no urgency, while the
product's listings have fewer than 10 views in all, or after two checks of a
listing in a row without a pass: a listing nobody sees can't show whether its
photo works, and in the analysis of 0.38.0 "The critic passes it" took 8 of
the 10 cycles #257 to #266 ($2.84) while ten checks scored 5 or 6 and a
listing had 0 to 4 views. Since 0.39.6 they wait for views while the listings
have fewer than 10 (waiting *views*), as a chore they still took cycles: on
10-15 five ($1.95) made new covers for listings 1 to 3 people had seen. A
defect still comes first. A step
carries the weight of the most important step waiting on it (since 0.35.1 a
promise is a step of its own, not carried). Since 0.35.2 the
[diagnostics](#diagnostics) report lists every one of these numbers, as your
version runs them.

**What Ember may change.** With her **plan_step** tool she adds steps to a
product (to a stage, or before a step), splits a step into smaller ones or
replaces it, says a step she added is done (a step with a check closes only when
the check passes), and says a step waits on something Ember's code can check:
your answer to a request, an upgrade request, another step or a day at most 14
days ahead (two a day at most); her code lifts the wait once the block is gone.
Since 0.37.6 a step whose own requests wait for you, and one a cycle took
without anything moving, wait by themselves (the brake, above): those take none
of her two. Since 0.39.1 a split, a replacement, done or a wait with no step
named means the cycle's own step (four calls were refused for naming none), a
step can't wait on itself, and in a venture cycle she can make a step of her
ventures (a triage, a brainstorm) wait for a day. Since 0.39.2 a step she says is
done needs a reason of more than one word (one was closed with "placeholder"),
and she is told that a step of the kind *market* (a pin, a post, a blog post) is
a marketing cycle's: a step for pins added as another kind went to an ordinary
cycle, which has no pin tool. Since 0.40.1 a step keeping one of her promises can
wait on a day, at the latest two days before the promise is due (from then on it
is urgent: she keeps it, or tells you why she can't). Until then it couldn't wait
at all: three cycles took "Report milestone #18 outcome (sale or park) by 10-31",
which can't be kept before the milestone is judged on 10-24, and each was told to
write to you about it. Your decisions' steps can't wait, as before. Since 0.40.2
a promise of no product line can wait the same way, in the cycle whose step it
is: the promise to propose the Kassenbuch by 10-25, which KDP's weekly limit
held until then, could only be tried and refused, a cycle a day. Since 0.40.3
she can't make a step you pinned wait (as she can't hold or skip it), nor a step
wait on one that waits on it (only a step's closing lifts such a wait), and her
two waits a day count from your midnight, not from midnight UTC. Since 0.42.6
her answer names the step it changed with its title (a wait meant for a promise
waited the cycle's own step, a venture's research, and "Step #251 waits on
2026-10-23" didn't say so), a third wait in a day says the step can wait from
tomorrow (a cycle read it as "already waiting"), and a promise to propose a KDP
book waits while KDP's week holds its format, as the book's own step does.
What you, a promise or Ember's code put in the plan stays. She can **hold** a
product to work on others, with her reason, and resume it. She can't close,
drop or delete a project or product: only you do. Each change is kept with its
reason, and you see today's under **Changes today**.

**Decide-by dates** (in place of the listing test, 0.13.0 to 0.34.0). Once a
product has a listing live, Ember's code reads its numbers on these days,
counted from the first day it was seen live:

| Day | Bar | If missed |
|---|---|---|
| 7 | 10 views | its marketing comes first for a week |
| 14 | 30 views and 2 favorites | with too little reach (fewer than 3 things done to bring buyers), one more try until day 28 with its marketing first; otherwise you decide |
| 21 | a first order | you decide; with an order, Ember gets a step to scale it (5 variants or a bundle) |

"You decide" is a step of yours on the product: **Keep it** or **Drop it**. A
test begun before 0.35.0 keeps its start and what its bars found. While you park
or kill the product's venture, no date is read.

**The Plan tab** shows:

- **your goal**, with a large bar of how far it got, its pace and its date;
- **the next cycle's step**, why it is taken, the three after it and what waits
  on you, a venture's step naming its venture; (0.36.0) **Titles and tags**,
  your freeze of the listings' titles and tags; and **Ventures**: how many are
  being explored and the heaviest of their steps, their worth, **Hold new
  things** or **Resume**, and **Their steps**, which leads to them;
- **the network**: your goal, each project in its own colour, each product as
  big as its worth. Choose a product to open its stages and steps below it. Drag
  to move it; Ctrl (or Cmd) and the mouse wheel zoom; **Fit** puts it back;
- **the product you opened**: its stages and steps with their state and weight,
  its numbers, its worth, its test's dates, Ember's hold (**Lift the hold**) or
  yours (**Resume it**), **Hold it**, **Close as done** and **Drop it** (with an
  optional reason), and its
  **Autonomy** box: what Ember's code may carry out for its listings without
  your click;
- **Changes today**, **Channels** (each channel's steps across the products,
  what comes next and what it did in the last 14 days), **Upgrades the plan
  waits on** (each upgrade request with the steps waiting on it and what they
  would weigh), and **Recent cycles** (the step each took and why);
- **Milestones**: yours and those Ember's code sets (a venture's first test,
  the money goal), as cards by horizon. The tab's badges count overdue
  milestones (▲) and the new dates Ember proposed for yours (◔), and (0.37.0)
  the business cases waiting for you (◔);
- (0.37.0, the Ventures tab until then) **Ventures** and **Product lines**: the
  venture tree with **Their steps in your plan** (each venture's step with its
  weight or what it waits on, what it needs now and **Explore next**; the name
  opens the venture's card), and the projects. See [Ventures](#ventures).

**Your part.** **Pin** a step to have the next cycle take it. **Set worth** gives
a product your own worth, from 0.5 to 10, in place of the one Ember's code
computes; **Use Ember's code's** takes yours back. At a decide-by date, **Keep
it** or **Drop it**. **Close as done** or **Drop it** ends a product (and its
line) whenever you want (since 0.40.3 with the promises and decisions owed on
it: "line closed by your owner"). **Lift the hold** puts a product Ember held
back in the plan. Since 0.40.3 **Lift the wait** takes off a wait Ember said for
a step (pin it too, to keep her from putting it off again). Since 0.36.0 **Hold it** holds a product yourself, with an optional
reason: its steps wait until you **Resume it**, and Ember can't lift your hold
(her own she can). Since 0.39.3 that holds the promises Ember made you about it
and your decisions on its requests too, as your park of its venture does: until
then they took cycles on the product you held (a rejection on its day, a promise
each day as its day neared). No cycle is aimed at it, and Ember's tools refuse
its work (a request, a listing, a pin or a post; she can still note its record),
since 0.39.7 in a venture cycle too: one filed a request about a product you
held. Since 0.42.1 they refuse its files too: a draft, a workshop run or a
document made from a file of the product, a change of its files, and a request
or an email naming its listing (reading them stays possible); the same holds
while a review's stop holds the line, until you keep it or Ember lifts the hold.
**Hold it** names what waits with it. Ember's own hold leaves what she
owes you going on. **Hold new things** on the Ventures box is "nothing new":
no venture cycle and no new product until you **Resume**. Ember's code refuses
a new product whatever venture it names, and (since 0.39.3) a new venture:
until then a new product of an idea went through, and an ordinary cycle could
make the idea. You can still add an idea yourself.
**Explore next** on a venture's step has the next venture cycle
take it (0.37.0). **Freeze** on the Titles and tags card
keeps the live listings' titles and tags as they are until the day you choose
(at most 60 days ahead): Ember's code refuses a change of them, the quality
critic asks for none, and Ember's plan says so; **Lift the freeze** ends it.
Live, "no title or tag edits before 10-20" was a sentence in the standing
instructions that the plan and the critic never read. Your **rulebook** is for
how Ember works (languages, style, what to avoid); what comes next is the
plan's. Since 0.39.6 Ember reads each rule with the day it was set, in UTC
("set 2026-10-07, 8 days ago"): rule #1's "This week ... 3) nothing new", set in
the night to 10-08, still held her back on 10-15, quoted as "the owner's rule
this week". Remove a rule that has done its job. **Add milestone** puts a milestone of yours under the goal or the
milestone you choose; it stays yours: only you drop it, and Ember can't move its
date (she may propose one; **Accept new date** or **Keep the date**). **Note**
leaves a comment on a milestone, and **Drop** takes an open one (and the open
milestones leading to it) off. Ember reads your word on her next wake.

**The upgrade to 0.35.0.** At the first cycle Ember's code moves the milestones
the tree takes the place of: the listing test's bars and scale points (each
product's decide-by dates keep their start and what the bars found), the goal's
decision points, and Ember's own milestones (one about a product becomes a step
of it; the rest are closed). The unlocks you gave on those milestones move to
their product's Autonomy box first. Each move is in the System log. From then
on Ember sets no milestones of her own: her steps are the plan's.

**How far each milestone got.** Every milestone shows a share from 0 to 100%:

- one with a **metric** (below): its reading against its target, as Ember's code
  read it ("$340 of $1,000", "12 of 30 views");
- **Ember's own goal**: the last 30 days' revenue less expenses against what it
  needs;
- one without a metric that milestones lead to: the mean of theirs, open and done
  ones ("1 of 3 steps done"); dropped and missed ones don't count;
- one without either: nothing until it is done (then 100%).

A ceiling (API spending at most so much) shows what it used instead. An open,
measured milestone also shows its **pace**: ahead of, on, or behind the share of
its time gone (since it was set), and its bar marks where a straight line to its
date would be today. The agent sees your goal in every plan, first in YOUR PLAN, with how far it got
and its pace, then the milestones still open with their share.

**Milestones Ember's code checks.** A milestone can name a **metric** and a
target, and then Ember's code checks it from its records, with no model call:
after each Etsy sync and before every plan. It closes the milestone **done**
once the target is met, with the numbers as its evidence, and **missed** once
its date has passed without it, with the numbers too (and, for orders and
favorites, whether the listings had too few views to judge). The agent can't
close such a milestone done; the database refuses it as well. Past its date,
Etsy's numbers read only after that day (a gap in the syncs) don't meet it: its
last reading by its date decides, and without one it is missed. Orders carry
their own date: those placed by its date count, however late they were read.
The metrics:

| Metric | What it counts | From |
|---|---|---|
| `listings_live` | the agent's listings live on Etsy now | Etsy |
| `views_delta`, `favorites_delta` | views or favorites gained since it was set (only while `etsy_stats_history` is on) | Etsy |
| `orders_observed` | Etsy orders of the agent's listings since it was set | Etsy |
| `revenue_verified_usd` | revenue less expenses recorded since it was set (your goal in total: since its day) | the ledger |
| `revenue_month_usd` | revenue less expenses recorded in the last 30 days (your goal a month; a leg's income too) | the ledger |
| `research_calls_ok` | research calls that found something since it was set | Ember |
| `case_complete` | the venture's business case is complete (researched, scored, filled in) | Ember |
| `stage_reached` | the venture reached a stage (researching, proposed, building, live) | Ember |
| `api_spend_usd` | API spending since it was set: a ceiling, missed once passed, done at its date | Ember |
| `qa_clean` | every live listing has at least 5 photos | Etsy and Ember |
| `views_total`, `favorites_total`, `orders_total` | the listings' views, favorites or orders in all, as Etsy counts them now (only Ember's code sets milestones with `orders_total`) | Etsy |

The Etsy listings Printify made for Ember's products count in these metrics
too, and so in the venture rules; `qa_clean`
leaves them out (their photos are Printify's mockups).

A milestone linked to a project or venture counts only what belongs to it (a
listing belongs to the project of the request that created it), and the links
of a milestone Ember's code set or checks never change. Etsy's numbers
count only from a sync after the milestone was set and at most 3 hours old; a
milestone for a killed venture's stage or case is closed missed. Its card on
the Plan tab shows where it stands (**Checked by Ember's code**), and the
agent's plan says what Ember's code closed since its last cycle.

**Forecasts Ember's code settles.** The agent gave its own metric milestones
odds of being met by their date (0.13.0 to 0.34.0: 5 to 95%); those still open
settle as before. When you back a venture, its
business case's days to the first sale become a 50% call. Ember's code settles
both from its records before every plan, with no model call:

- a milestone call **came true** if the milestone was met by its first date (a
  date moved later doesn't move the call). It **didn't** once that date has
  passed or the milestone was missed. It is **void** if you or Ember's code
  dropped the milestone first; the agent's own drop settles it as a miss;
- a first sale came true once an Etsy order of the venture's listings, or
  revenue recorded for it, falls within its time (a sale recorded up to 7 days
  after that still counts, if it happened in time).

A call never changes, and a settled one is final. The record in a few words
(how often the milestones given odds were met against the odds given, with the
Brier score, where a coin toss scores 0.25, and how many first sales came on
time) reaches the critic and the daily review. You
see it with the milestones on the Plan tab, with each call on its milestone's card, in the
ventures' summary (the Ventures tab's Decision desk until 0.37.0), and on a backed venture's card.

**Money, time and waiting.** A milestone can carry what it may cost: the API
spending the agent plans for it, the cash it needs from you (EUR) and your
hours. These are set with it and can't be raised later: the plan shows "spent
$0.42 of $1.00", and Ember's code flags a milestone that spent more than its
budget. What counts toward a milestone is the work of the cycles aimed at it;
their plans, the daily reviews, brainstorms and library study are overhead,
shown as one line under the milestones on the Plan tab. Every model call records the venture and
milestone its work served (a research call for another venture counts for that
venture), and every request to you records the venture and milestone of the
cycle that made it, so a venture's "spent" is its own work, not whole cycles. A milestone can also **wait**, for your
decision or for buyers, until a day at most 14 days ahead: while it waits it
isn't flagged as overdue, and on the morning its check is due (08:00 your time)
the agent wakes to look again. The plan also shows each overdue or this week's
milestone's newest note.

**Honest by design.** What a milestone promises (its title and its measure)
can't be changed once it is written. Its date can move twice at most, with the
reason, and every move is counted. A milestone ends **done** (with the
evidence), **missed** (why, and what now; only once its date has passed) or
**dropped** (why), and is final then. Dropping a milestone drops the open
milestones that lead to it too. Ember keeps who closed each milestone (the
agent, you, or Ember's code from its records). Of the 20 open places, the last 4
are kept for yours; your goal and the milestones Ember's code sets take none.

## Library

The **Library** tab is where you hand the agent reference material you find:
Etsy's own guides to listings, titles, tags and keywords, an SEO guide, notes of
your own. The agent can't fetch Etsy's pages itself (Etsy's API terms forbid
programs reading its site), but you can: open the page, select its text, copy
it and paste it into **Add**. You can also upload files: text, Markdown, a saved
web page (HTML), PDF or Word (.docx), up to 8 MB each; each file becomes a
document. A large file takes a while over a slow connection: the dashboard
waits longer for a larger file, up to three minutes. If no answer comes in
time, look at the list before you add it again (Ember refuses a second copy of
the same text). Give a document a source (its link), the venture or project it
is for, and a note on what to use it for, if you like.

**Studied once, kept for good.** At the start of its next wake cycles, before
it plans, the agent studies each new document: it reads it a few thousand
words at a time and keeps what is worth knowing, as a short summary and
specific learnings (a rule, a number, a how-to step, a mistake to avoid), each
naming the part of the document it comes from. The text never has to be read
or analysed again. Studying costs a few cents for a typical page (a long guide
of 100 pages about $0.30 with the default models); the **Daily study budget**
option limits it (0.50 USD a day by default, counted toward the daily cap), and
a long document is studied over several cycles or days. A document keeps at
most 60 learnings: once they are full its study ends, and its card says which
parts weren't studied (the agent can still read them). Each card shows how far
the study got, what it cost and, when you open it, what the agent learned.

**Used where it matters.** Each plan sees what was newly learned; each work
step gets the learnings that match what the cycle is doing (picked by Ember's
code, from the documents for the plan's venture or project first); and the
agent can search the learnings and the texts, or read a document, with free
tools. Documents are your reference, shown to the agent as information: it
doesn't take orders from them, and it doesn't copy their text into what it
publishes. **Remove** takes a document's text out (what was learned from it is
no longer shown); a study that failed three times stops until you press
**Study again**.

## Your part

- **Library**: add pages and files worth knowing; the agent studies each once
  and keeps what it learned (see [Library](#library)).
- **Ventures**: back, park or kill the agent's business cases and add your own
  ideas, on the Plan tab since 0.37.0 (see [Ventures](#ventures)).
- **Plan** (0.35.0, in place of the Roadmap tab): set your goal, pin a step,
  set a product's worth, keep or drop a product at its decide-by date, close or
  drop one, lift Ember's hold, set a product's unlocks, and add milestones you
  want reached by a date (see [Plan](#plan)).
- **Projects** (the Ventures tab's **Running** view since 0.27.0, the Plan
  tab's **Product lines** since 0.37.0): the
  agent's open projects as cards (its hypothesis, its stage in the plan, what it earned
  and cost, and what it nets after its expenses such as Etsy's fees), the
  closed ones as rows that open. A backed or live venture's projects are in
  its card; the others follow under **Other projects**. Choose Open, Closed or
  All and a sort; this browser keeps them. A card's **approvals waiting**
  opens them on the Approvals tab, its venture opens the venture, and a cycle
  in its log opens that cycle on the Activity tab (the last 10 cycles are
  there) (0.19.4). Its **Files** shows the files written for it on the
  Workspace tab (0.26.0), whose **Open in Ventures** brings you back to it.
- **Approvals**: anything that leaves the container (publishing, contacting
  someone, creating an account, spending money, selling) arrives as a request.
  Approve it, approve it with your own changes to the text, or reject it, with
  an optional comment. An approval does nothing by itself: *you* carry it out,
  then mark it **done** (with a link or note) or **failed** (with what went
  wrong). If it cost money, record the expense in the ledger; if it earned
  money, record the revenue. Three kinds are different: an approved **email** is
  sent by Ember itself (see [Ember's mailbox](#embers-mailbox)), an approved
  **Reddit post** comes with a button that opens Reddit with the text filled
  in (see [Reddit](#reddit)), and an approved **KDP book** (0.25.0) comes
  with its files, Copy buttons and a button that opens your KDP Bookshelf (see
  [Amazon KDP](#amazon-kdp)). A request you don't decide **expires**
  (emails and posts after 7 days, spending after 14, the rest after 30; the
  card says when; 0.37.3: checked every minute, before your unlocks act: only
  a wake cycle's start expired them, and on Resume after a long pause an
  unlock approved a reply past its days), the agent can **withdraw** one that
  is outdated (with its reason), and each kind has its own limit of waiting
  requests (6 sales, 5
  emails, 3 posts, 3 accounts, 3 spendings, 4 others), so waiting listings
  never block an email reply. Since 0.37.9 Ember's plan knows the posts' limit
  (since 0.38.1 the sales' too): while it is full, a pin or post (a listing, a
  change, a product) waits instead of a cycle being paid for that the limit
  refuses (see [Plan](#plan)). Each card says what kind of action it is
  (0.13.0) and flags what matters for your decision: whether it reaches people,
  can be a first contact, costs money, appears under your name, and whether
  Ember's code can undo it. A listing, or a change to one, with fewer than 5
  photos (the one **QA** number: the Etsy guide, the plan's defects and the
  `qa_clean` metric read it too) says so on its card before you decide.
  Since 0.42.0 the card of a listing, a change, a pin, a post or a Printify
  product says which of its files changed since it was proposed (as a KDP
  book's does): Ember uploads only the files as proposed, so **Approve** is
  refused for it; reject it and ask the agent to propose it again.
- **Asked to wait** (0.42.0): when Etsy, Pinterest or Bluesky (0.42.3: or
  Printify) answers "too many requests" (or "down for now, come back later")
  before anything changed (no draft, no part of a change, no new board, no post
  yet; at Printify, no picture uploaded yet, nor a change made), the approved
  request stays approved: Ember tries it again when the service said, at least
  a minute and at most 6 hours later, with every check again (the kill switch,
  a pause, the files, the daily limit, the publishing queue), at most 3 times,
  then it fails as before. Its card says when; **Cancel** still stops it. A
  request your unlock approved isn't tried again (the agent proposes it again),
  nor is an email, nor anything that may have changed something: a Printify
  product that Printify refuses once its picture went up (the picture stays in
  your Printify library) fails as before. Since 0.42.4 your **Undo** of a pin,
  a Bluesky post or a Printify product waits the same way when the service
  gives that answer to its delete (nothing was deleted then): until then the
  answer failed it, and you had to press Undo again. Its card says when it is
  tried again ("Pinterest asked to wait"), the kill switch holds it, it is
  carried out while Ember is paused too, and **Cancel** stops it (you can
  press Undo again). After 3 waits it fails as before. Your Undo of an Etsy
  listing's action is a change of the listing, which waited already.
- **Approved too long ago** (0.42.2): a listing, a change, a pin, a post, a
  Printify product or change, or a blog page that Ember's code hasn't begun 7
  days after your approval isn't carried out any more, as an email isn't (see
  [Ember's mailbox](#embers-mailbox)). Ember was paused or stopped, or it
  waited for money, a connection, a daily limit, its slot or its service, and
  it may no longer fit: Ember closes it as failed, with why ("Not listed: it
  didn't go out within 7 days of its approval ..."), and the agent hears it
  and proposes it again if it still fits (its plan gets a step for that). One
  that waits for its service ages out too, before it is tried again. Since
  0.42.3 also one Ember's code can't carry out at all (your account is
  disconnected, Bluesky refuses the login, Printify has no shop, Ember's
  mailbox can't be read): until then its card said approved for as long as
  that lasted.
  What Ember's code began never does, nor your Undo (it takes something down,
  which still fits), nor what you carry out yourself (a Reddit post, a KDP
  book), Pinterest's test pin or the last will.
- **Autonomy** (0.13.0): on the Plan tab, each open product (0.35.0) and each
  open milestone's card has an **Autonomy** box. There you can let Ember's code carry out a few small kinds
  of request for that milestone without your click. Each is off (**Ask me**)
  until you choose otherwise (0.15.0: an unlock carries only what belongs to
  its milestone, whatever the plan works on: a milestone of a project covers
  that project's listings, one of a venture the listings of its projects, and
  one of neither covers email replies, so the box offers only those rules;
  it ends when its milestone closes, also when it is met, and what it held
  waits for you; 0.21.0: while it stands, nobody can change the milestone's
  project or venture, and no project moves into or out of the venture of an
  unlocked venture milestone, so the agent can't widen what it covers; take
  the unlock back to change them):
  - QA fixes: up to 5 distinct photos (no copies) on a live listing;
  - price changes within 15% of the price you last approved on a live listing
    (several automatic changes can't add up past it);
  - new listings in a backed leg, once you approved 5 of its listings
    unchanged;
  - taking a listing of Ember's off Etsy;
  - email replies in threads the other person started, only with the veto
    window (0.22.0: Ember's code checks a reply's thread and words, not what
    it may quote of other people's mail, so none goes out at once; an
    unlock of them on **Run at once** was taken back at the upgrade).

  **Run unless I veto within 12 h** holds such a request on its card with the
  time it will be approved, and you can still reject it. While Ember is
  paused or waits for money no unlock acts, and its card says it is approved
  12 hours after Ember runs again: once it runs, each request held this way
  gets the whole 12 hours again (0.37.3: one whose window ran out during a
  pause was approved the minute you resumed Ember). **Run at once**
  approves it when it is made. Each rule has a daily limit and a budget of
  actions. An unlock carries only a request that passes the checks its card
  shows (QA: a reply keeps "Re: …" and has at most 200 words, a listing has at
  least 5 photos); any other waits for you, and a held one's card says why
  (0.15.0). Unlocks need your user ID in `owner_user_ids`: without it, or in
  safe mode, you can't grant one, the Autonomy box says why, and Ember's code
  takes back the ones you granted (grant them again once it's fixed). Ember's
  code takes an unlock back itself when one of its actions ends unclear, its
  budget is spent, the milestone closes (met, missed or dropped), or you veto
  or cancel one of its requests. It never widens one. When you approved 5
  requests of a kind unchanged and without a comment within 30 days, the
  Plan tab suggests an unlock, and you decide (0.15.0: not again within 30
  days after Ember's code took that unlock back for a reason other than a
  spent budget). Ember hears each change in its news, Ember's code's
  take-backs too, and its plans and the milestone's card say what stands
  unlocked (an unlock is no longer written into your note on the milestone).

  **Never automatic**, whatever you unlock: creating an account, moving or
  spending money, a first contact (someone who never wrote to Ember, § 7 UWG),
  Ember's first listing in your shop, the pin that makes its first Pinterest
  board and its first Printify product (a new public presence needs your
  decision and your Impressum, § 5 DDG; physical goods bring duties of their
  own), a post in a third-party community, an email whose text touches tax,
  VAT, an invoice, a Gewerbe or a contract (0.15.0: read in what it sends,
  look-alike letters, hidden characters and hyphens inside a word included,
  in German, English and the neighbours' words for VAT, invoices and
  contracts, offers and quotes, not in a listing's copy; a word list can
  still miss a phrasing or catch a harmless word, such as a licence or an
  offer in a reply), and anything only
  you carry out. Such a request always waits for your click, and if it fits an
  unlocked kind, its card says why it waits. Only you unlock: Ember's code
  can only take an unlock back. The database checks this again on its own. It
  refuses an unlock's approval of such a request, an approval that no current
  unlock covers, and anything beyond an unlock's budget. When an unlock is
  taken back, what it was holding waits for you, and so does what it approved
  that Ember's code hasn't begun (its card says why). What began runs on. An
  unlock that ends because its budget is spent takes nothing back: what it
  approved runs.
- **What Ember's code did**, below the requests on the **Approvals** tab
  (0.13.0): every action it carries out (an email sent, a listing created,
  changed, renewed or deactivated, a sold listing's automatic renewal turned
  on, a Pinterest board or pin made or deleted, a Bluesky post made or deleted,
  a Printify product created or deleted), the newest first. Each one shows:
  - what it acted on, and what it changed (before → after);
  - how it ended (0.42.2: **Asked to wait** when its service asked Ember's
    code to come back later, and it is tried again: see Asked to wait above);
  - on whose decision: **You approved it**, **Your unlock**, **Your Undo**,
    or **Ember's code on its own**.

  **Undo** reverses an action on one of Ember's listings. It deactivates a
  listing Ember created, changes a changed listing back, renews a
  deactivated one (Etsy's fee again) or turns an automatic renewal off. The
  Undo is a request of yours, approved at once. Ember's code carries it out
  in its next round, like any change you approve, and the entry shows its
  request. Only the newest action on a listing can be undone, and not while
  another change of it waits; an action that was undone no longer counts, so
  after an Undo the action before it can be undone too (0.15.0). If you undid
  an Undo, the entry names the action to undo next. An Undo is
  carried out while Ember is paused or waits for money too, but not while the
  kill switch is on: that stops everything Ember's code sends, so undo it at
  Etsy by hand. An email can't be unsent. A pin's **Delete the
  pin** deletes it at Pinterest (a board stays: delete it at Pinterest if you
  want). A Bluesky post's **Delete the post** deletes it at Bluesky (0.19.0).
  A Printify product's **Delete the product** deletes it at Printify,
  which takes its Etsy listing down too (check your shop). If an Undo failed
  before anything changed, or you cancelled it, you can undo again (0.42.4:
  one the service asked to come back later for waits first: see Asked to
  wait above). If the app stopped while deleting a pin or product, the Undo
  ends unclear and you can press it again (what is gone already counts as
  deleted).

  Before an unlock's price change or photo fix is made, Ember's code reads the
  listing at Etsy again (0.15.0). If its price differs from Ember's record,
  or its photos aren't the ones Ember's code set last (you changed them at
  Etsy, or Ember never noted them: listings from before 0.15.0), nothing is
  changed and the change comes to you as a new request.

  **Daily digest**: at the start of each day, Ember's code sums up the day
  before. It covers how many actions it carried out and on whose decision,
  what failed, what your unlocks approved, what they hold for your veto,
  which unlocks were taken back and why, and what waited for you whatever
  you unlocked. The newest digest is at the top of this card and in the System
  log; the sensor has its counts (`digest_day`, `digest_actions`, ...). Days
  without a round (a pause) get their digest afterwards, up to 14 days back
  (0.15.0). Since 0.42.2 a try that Etsy, Pinterest, Bluesky or (0.42.3)
  Printify asked Ember's code to make later (see Asked to wait above; 0.42.4:
  your Undo's too) is no failed action: the digest counts the requests that
  waited apart
  ("1 request(s) waited when their service asked to come back later (not
  failures)"), a listing that waited twice and then went out is 1 action, and
  one that fails at its last try fails once. Since 0.42.3 an approved request
  that went back unsent because Ember's code didn't begin it within 7 days of
  your approval (see Approved too long ago above, and for an email Ember's
  mailbox) is no action and no failure either: the digest counts it apart,
  with whose approval it had ("1 request went back unsent (1 you approved): it
  didn't go out within 7 days of its approval (Ember proposes it again if it
  still fits)"), and `digest_actions` and `digest_failed` leave it out. Until
  then it read "carried out 1 action (1 you approved): 1 failed", though
  nothing was carried out. The list of what Ember's code did has no entry for
  it (nothing began; a try that waited for its service before shows there as
  waited), and a request that really failed still counts as failed.

  **Take back every unlock**, at the top of this card while any unlock
  stands: one click sets every rule of every milestone back to **Ask me**.
  What the unlocks held for your veto, or approved without Ember's code having
  begun it, waits for your click again, and Ember hears it in its news. 0.15.0
  took back every unlock of 0.13.0 once: grant again what you want.
- **Rulebook** (since 0.36.0, in place of the standing instructions in the
  **Inbox**), the first card of the **Mind** tab, above the agent's own notes:
  your rules for how the agent works (languages, style, what to avoid), each with its
  number, which it reads in every plan and work step, so you don't have to
  repeat them in messages. **Add rule** adds one (at most 500 characters; 20
  rules and 1,500 characters in all); **Edit** gives one new words, which become
  a new rule in its place; **Remove** (click twice) takes one out. Each rule
  holds until you remove it, and the history keeps every one. The standing
  instructions were one text: live, a new one for the week's order of work
  (2026-10-07) replaced the rules set before it, and they dropped out with it.
  What comes next belongs to your plan (its pins, holds, worths and the freeze
  on the Plan tab), and one-off things to messages. At the upgrade your current
  standing instructions become your first rules, one a paragraph (and one a
  line that starts with "- "): read them once, shorten or split them, and move a
  week's order of work to the Plan tab. Since 0.33.0 each of your messages has
  **Keep as rule** (**Keep as instruction** until 0.36.0): it opens the Mind tab
  with the message in the form, to shorten to the rule. A message leaves the agent's plans once
  it is answered (live, "Bluesky posts in English only" was acknowledged in the
  morning and a German post proposed that evening); a rule stays in every plan.
  Since 0.42.6 an answered message stays 3 days after you wrote it (the newest
  two), marked answered and that any work it asks for is still Ember's, after
  everything else and only in the room left: your three fixes to the Kassenbuch
  left every plan once Ember had answered "the next cycle applies them", and no
  cycle made them.
- **Inbox**: the agent's messages to you, and yours to it. Your message wakes
  it to read it (see the option **Wake Ember when you write**): 5 minutes after
  your last message or decision (at most 15 after the first, at least 30
  after the last wake for your news, and a restart keeps the wake), so one
  cycle reads all of them. A cycle that comes first reads them too. If it is in
  the middle of a cycle, it wakes after the cycle, unless that cycle already
  saw them. With the option off, or while it is paused or dormant, it reads
  your message at its next wake-up. **Wake now** wakes it at once. Your reply
  also marks its earlier messages read; while five of its messages are unread,
  it can't write to you, and it sends at most
  two messages a day that answer none of yours (Ember's code counts them),
  plus one per promise that reports it kept (naming its #number) so it can
  close it. Since 0.37.9 its plan holds any other report back once the two are
  sent, until the next day or until you write. A
  message that promises something shows the promise under it, and whether it
  was kept. Never send
  passwords: messages are stored and sent to Anthropic, and the agent can't log
  in anywhere (the page warns you when a message looks like a login). **Remove
  text** blanks one of your own messages for good, for example a password
  sent by mistake; a note that something was removed stays. Each of your
  messages stays in the agent's plans until one of its messages answers it (and
  since 0.42.6, marked answered, up to 3 days after you wrote it),
  so a cycle that ends early can't lose your question; under your message the
  Inbox says whether it was answered yet. The Inbox shows the newest 30
  messages and every one of yours still waiting for an answer; **Show older
  messages** loads the ones before. The conversation scrolls in its own box and
  stays at the newest message while you are there; scrolled up, a new message
  doesn't move what you read (**↓ Latest** brings you back). Days are marked,
  and a line shows where the unread messages begin. **Answered** under your
  message shows the answer. **Waiting on Ember**, beside it, lists your
  messages it hasn't answered and every open promise, the overdue ones marked;
  click one to see its message (0.19.4).
- **Workspace**: the files the agent writes in its own folder (drafts, notes,
  research), and the PDF, Word, Excel and picture files Ember made from them, so
  you can review them before you approve anything (0.26.0). Ember's code files
  each file under the project or venture it was written for: the project the
  agent's plan focused on when it wrote the file (or the venture a venture cycle
  studied). A file keeps the project it was first written for; one written
  without a focus goes under the next focused cycle that writes it; a product's
  Word copy and pictures go with it; a venture's knowledge file is always that
  venture's. Files from before 0.26.0 were filed once from the agent's tool
  calls that wrote them. At the top, an overview: how many products and text
  files there are, the last change, how much of its space the workspace uses
  (text files may take 50 MB, products 2 GB, 5,000 files in 1,000 folders), the
  files in requests that wait for your decision, **By project**, a card for
  each project and venture with files (what it has, pictures of its latest
  work, the newest first; a venture counts its projects' files too; click one
  to show only its files), and **Latest work**, the newest items, those changed
  since you last looked in this browser marked **New**. Below, **All files**: a
  product's files are one item (a document's PDF, its Word copy and its page
  pictures; a spreadsheet and the pictures of its sheets), shown with a small
  picture of it. Sorted **By project** (the default), a venture's own files
  come first, then a group for each of its projects; a project of no venture
  is a group of its own, and the files filed under none come last. Search by
  name, folder or project, show only documents, spreadsheets, pictures or text
  files, choose a project or venture and a folder, sort by project, folder,
  newest, name or size, and switch between a list and a grid (the kind, the
  sort and the layout are kept in this browser). The list loads again by
  itself after each of the agent's steps while the tab is open. Open an item to
  see it in a viewer: its project and venture and the cycle that last wrote it
  (each opens on its own tab), a product's pictures (a document's first pages,
  every sheet of a spreadsheet; each opens at full size), a download for each
  of its files, the requests it is in (**Open in Approvals**), and the text it
  was made from. A text file shows formatted (Markdown with its headings, lists
  and tables, a CSV file as a table, JSON indented) or as plain text, and you
  can download or copy it. It is never shown as a web page: a link shows its
  address, and HTML stays text. **←** and **→** step through the list you
  opened it from; Escape closes it. On a request in Approvals that names files
  of the workspace (a listing's photos and files, a book's, a pin's picture),
  **View the files** opens them in the same viewer. Check a file before you use
  it. In dry run you see the dry-run folder.
- **Etsy listings** the agent proposed are approvals too: after you approve
  one, Ember creates it in your shop (see [Etsy](#etsy)).
- **Upgrade requests**: ideas for changing Ember's code. Accept, decline, or mark
  one released with the version that contains it. After an update the agent
  reads what changed in the release notes, about 2 KB a plan until it has read
  them all, the newest first. Since 0.32.0 a version installed while it reads
  adds its notes first, and the rest go on where they stopped (each update
  began them again from the top, so with frequent updates the older notes were
  never read). A request built on a workshop script
  comes with the script (see [How Ember grows](#the-workshop)).
- **Pause / Resume** stops and restarts the wake cycles. **Wake now** starts a
  cycle right away.
- **Kill switch**: stops the agent for good (type its name to confirm). It
  takes back every unlock too, as **Take back every unlock** does, so its reset
  approves nothing an unlock held. The dashboard keeps working. To undo it,
  change **Kill switch reset** in the app's **Configuration** tab to any other
  number, save and restart the app. It also stops Ember's code in the middle
  of carrying out what you approved (0.37.3: 2 of 3 approved emails went out
  after the switch was pressed): what has begun runs on, once (an email being
  handed over, a listing being created), and nothing else begins. What you
  approved waits and is carried out after the reset (an email once Ember has
  read its mailbox). The live view isn't uploaded while the switch is on: your
  page keeps its last upload, whose time tells its readers after an hour that
  Ember is offline (delete its files at your host if you want it gone).

The agent hears about your decisions, messages and upgrades once, at its next
wake-up.

## Going live

1. Create an [Anthropic Console](https://console.anthropic.com) workspace just for
   Ember and set a monthly spend limit there (for example 31 × the daily cap).
   This is the outside safety net if anything in Ember went wrong.
2. Create an API key in that workspace and make sure the account has credit.
3. Compare the price table in the options with Anthropic's pricing page.
4. In the app's **Configuration** tab, turn on **Show unused optional
   configuration options**, paste the key, turn **Dry run** off, save and
   restart the app.
5. Record a grant if the starting balance isn't enough, and watch the first
   cycles. Anthropic's Console shows the real costs; if they differ from
   Ember's, record an API cost correction.

If the API refuses the key, reports a billing problem or a reached spend limit,
Ember stops calling it and says so on the dashboard. Fix the cause and restart
the app.

**Dry run or live?** The option **Dry run** is the only switch between the two.
While it is on, the agent talks to a fake model built into the app: nothing is
sent to Anthropic, nothing is spent, and the dashboard's header says *Dry run:
API costs are simulated*. With dry run off and a key set, that badge is gone,
the **System** panel says *Mode: Live (real API calls, real money)*, the fake
model is never used, and every call appears in your Anthropic Console. (If the
options are invalid, safe mode turns dry run back on and says so at the top of
the dashboard.)

## Ember's mailbox

Ember can have its own email address. It reads its mail on its own, but it can
only *propose* an email: Ember's code sends it after you approve it. The agent
never sees the password and has no way to send anything itself.

### Setting it up

A small paid mailbox at a German provider works best:
[mailbox.org](https://mailbox.org) Light (about 1 EUR a month) or
[Posteo](https://posteo.de) (1 EUR a month). Gmail and Outlook.com are poor
fits: Google discourages app passwords and may lock new accounts that send
automated mail, and Outlook.com no longer accepts passwords for IMAP and SMTP.

1. Create a new mailbox by hand, for example `ember-yourname@mailbox.org`, and
   pay for it (a trial account can't send to other providers).
2. Give it a strong password and turn on two-factor authentication for the web
   login.
3. Create an **app password** in the mailbox's security settings, limited to
   IMAP and SMTP if the provider offers that, and name it "Ember". Ember only
   ever gets this app password, never your main password.
4. In Ember's **Configuration** tab, turn on **Show unused optional
   configuration options** and fill in: **Ember's mailbox** on, the **Mailbox
   address**, the **Mailbox app password** and **Your name for emails**. The
   servers default to mailbox.org's; for Posteo use `posteo.de` for both, with
   ports 993 and 465. Save and restart the app.
5. Set **Your mail provider's authserv-id** (0.22.1), so that only your
   provider's verdict on a sender counts (see [Your provider's
   authserv-id](#your-providers-authserv-id) below).
6. The dashboard's **System** panel shows the mailbox's status, when it was
   last checked, the last error and how many emails were sent today.

To cut Ember off at once, revoke the app password at your provider (or switch
the option off). Home Assistant backups contain the options, the app password
included: encrypt your backups.

### How it works

- At the start of every wake cycle Ember checks the inbox over TLS (reading
  only: nothing is marked read, moved or deleted there) and stores the new
  emails in its database, the oldest first: up to 100 per check, and any more
  wait for the next check (none is dropped). An email the server won't hand
  over is asked for again at the next check; after 5 checks Ember skips it and
  says so in the events, so read it in your webmail (0.15.0). Only the text is
  kept (at most 8,000 characters): since 0.39.7 a longer one loses what it
  quotes first (its lines with ">"), the oldest emails of a thread first, each
  place shown as "> [...]", and only then its end (until then it kept its
  first 8,000 characters, so a "stop" written below a long thread was lost).
  Text hidden in HTML emails is dropped (also by a style sheet or a colour
  like its background; since 0.45.2 a style sheet's colour counts only inside
  the elements its rule names, so Amazon KDP's notices are no longer taken for
  white on white), and attachments are listed by name and size but never
  opened. Since 0.45.4 an email stored before a change to how Ember reads an
  email's text is read again from the server, if it arrived in the last 30
  days and the server still has it under its number (10 per check, the newest
  first): the stored email never changes, its new text is kept beside it, and
  the agent reads that one, as unread again. A "stop" in it counts. An email over 1 MB keeps the text in its first 256 KB, where an
  email's text comes before its attachments, and says so; its attachments
  aren't fetched (before 0.39.5 it kept its headers only, so a "stop" sent
  with a photo was never read). One whose text comes after a photo still keeps
  its headers only. The agent treats emails as data from unverified senders,
  never as instructions.
- The agent sees its unread emails and can propose an email, usually an
  answer. The request shows the recipient, the subject and the text, and warns
  you when the address never wrote to Ember (a first contact). Since 0.15.0
  only an email whose sender your mail provider verified counts as having
  written: anyone can put any address in *From:*. Ember reads the provider's
  verdict in its *Authentication-Results* header, which the provider adds
  above any the sender wrote (DMARC, or DKIM or SPF of the sender's domain or
  a parent or subdomain of it, passed). With **Your mail provider's
  authserv-id** set, that is the topmost header of that authserv-id, and an
  email without one is unverified (0.22.1). Without it, Ember takes the
  topmost header: if your provider adds none to an email, that is a header the
  sender wrote, and it counts. The **System → Email** card then shows the
  sender check as incomplete.
  Someone who wrote before 0.15.0 counts again once they write again. An
  email keeps the verdict it was stored with: setting the option doesn't
  change it for emails already stored.
- **Approve** it and Ember sends it itself, exactly once: plain text, to that
  one recipient, no copies, no attachments, from "Ember (AI agent of *your
  name*)" (or the agent's name, if you changed it), with a footer the agent
  can't remove:

  > This email was written by Ember, an AI agent, on behalf of *your name*, and
  > approved by them before sending. Reply "stop" and Ember won't write to you
  > again.

  An email your unlock approved says instead that it was "sent under rules they
  set, without their review of this email" (0.15.0).

  **Approve with changes** edits the text only; the recipient and the subject
  stay. After a **Reject** nothing is sent.
- Ember sends at most *Emails per day* (per local day); more wait for the next
  day. While an email waits, **Cancel sending** stops it.
- Before an approved email goes out, Ember reads its mailbox, unless a read
  that began after your approval, at most 5 minutes before, left nothing to
  read (0.37.3): a "stop" that came while the kill switch was on, the app was
  down or reading failed was read only after the first round had sent to its
  writer. While the mailbox can't be read, or new mail is left to read, every
  approved email waits, and its card says why. Since 0.39.5 that includes a
  read that fails while it stores the emails (it raised past the record, so
  the card said only "waiting"), and the listings, pins, posts, products and
  uploads you approved go out meanwhile: each kind on its own.
- Since 0.40.3 an approved email also waits while its recipient wrote again
  after it was asked for (from their address, or in its thread) and the agent
  hasn't opened that email yet: it may ask to stop in words Ember's code
  misses. Its card says so, and the agent is told in its next cycle. Once the
  agent has read it, it marks such a stop (then the email is never sent), or
  the email goes out. An approved email that hasn't gone out 7 days after your
  approval (Ember was paused, or it waited for money, the daily limit or an
  email to be read) isn't sent any more: Ember closes it as not sent, with
  why, and the agent asks again if it still fits (until 0.40.3 it went out on
  Resume, weeks late; until 0.42.3 one that waited for the mailbox to be read,
  or whose mailbox was switched off, stayed approved until it could go out).
  Since 0.42.2 the other approved actions Ember's code carries out age out
  after the same 7 days (see [Your part](#your-part), Approved too long ago).
- Ember then closes the request itself: done ("Sent … as *message id*"), or
  failed with the reason. If the mail server refused it (a wrong password, an
  unknown recipient), it wasn't sent. If the connection broke while the email
  was being handed over, or the app stopped in the middle, Ember can't know
  whether it went out: it says so and never sends it again. Check at your mail
  provider, or ask the recipient, before you send it again by hand.
- Whoever asks not to be emailed again is never emailed again. Ember's code
  catches it in the sender's own words (not in what they quote), in English,
  German, French, Spanish, Italian, Dutch, Portuguese and Polish: "stop" or
  "unsubscribe" alone, "remove me", "don't email me", "keine E-Mails mehr",
  "abmelden", an objection to the use of their data (GDPR Art. 21), and the
  like, also on its own line after a greeting ("Hallo, … Stopp."). Since
  0.39.5 also below what they quote or between its parts, as Thunderbird and
  German netiquette reply: only the words above the quote's first line ("Am …
  schrieb …:") counted, so such a stop was stored and the email you approved
  went out anyway. In an HTML email a quote's lines show with ">" too, so they
  count as quoted. Since 0.39.7 also below a long thread: Ember's code read an
  email's first 8,000 characters, what it quotes included, so a stop below a
  thread quoted three rounds deep was missed. A "stop" in answer to Ember's
  email also covers the address Ember wrote to, if someone else answered
  (0.15.0). A newsletter's or an automatic reply's "unsubscribe" doesn't
  count, unless it answers Ember's email (0.21.0: a "stop" sent through a
  group or a mailing list was ignored). Since 0.40.3 more phrasings count
  ("mich nicht mehr zu kontaktieren", "unterlassen Sie … jede weitere
  Kontaktaufnahme", "sehen Sie von weiteren E-Mails ab", "do not reach out to
  me again", "please remove"), a mail gateway's tag before the subject
  ("[EXTERNAL] STOP") doesn't hide it, a link elsewhere in the same line no
  longer does either (only one in the stop's own sentence marks a
  newsletter's footer), the HTML part is read when the plain-text part is
  empty or only stands in for it, and text in an unknown charset (or none)
  that isn't UTF-8 is read as Windows-1252, not as replacement characters.
  An email Ember can't read (one nested deeper or with more parts than any
  real email, or one the parser fails on) is stored with its headers only, and
  a "stop" in its subject still counts (0.21.0: one such email stopped Ember
  reading its mailbox for good). Since 0.39.5 so is one the database refuses as
  it is, with a warning in the events, and a sender's name in raw 8-bit bytes
  (spam often has one) is read as UTF-8: one such email stopped every read of
  the mailbox for good, and every approved request while an email waited.
  When a sender asks in words the check misses, the agent marks it
  (`mark_opt_out`). You can add any address under **System → Email → Never
  emailed** (someone asked you, by phone for example). An opt-out is final: the
  list can't be shortened. A false alarm only means Ember doesn't write to that
  person; they can still write to it, and you can answer them yourself.
- **People who write to Ember** (0.13.0): Ember treats each email from a
  person as waiting for an answer: a buyer's question, a reader, a partner.
  Newsletters, automatic replies, bounces and no-reply senders don't count,
  and neither does anyone who asked to stop. Since 0.15.0 neither do spam your
  provider flagged, mail from Ember's own address, or an email whose sender
  your provider didn't verify; emails stored before 0.15.0 never count. Ember's
  plan lists the waiting ones first, so they are answered before other work. A
  new one that comes in between cycles wakes Ember (see the agenda). An email
  stops waiting when Ember proposes an answer to that person. If you reject
  the answer, the email waits again. The agent can also close an email that
  needs no answer (`inquiry_done`, with the reason), for example a thank-you
  or spam.
- An answer in the thread of someone who wrote has its own kind on the card:
  **answer someone who wrote to Ember**. It is no first contact when their
  email was a verified person's (see above). An answer to an unverified
  sender, a list or a machine still gets the card's first-email warning, and
  an email-reply unlock doesn't send it. Ember's
  code checks two things, and the card and the agent both see the result:
  that the subject keeps the thread ("Re: …"), and that the answer is short
  (at most 200 words). On a milestone, the metrics `inquiries_received` and
  `inquiries_answered` count people's emails, and Ember's answers to them,
  from a milestone's start. If someone asks to stop after an answer your
  email-reply unlock sent, that unlock is taken back.
- In dry run a built-in fake mailbox stands in: a reader asks about a German
  version of a planner in the first cycle, a newsletter with hidden
  instructions arrives in the third, and the reader's "stop" in the fifth.
  Approved emails are recorded, not sent.

### Your provider's authserv-id

Every *Authentication-Results* header starts with the name of the server that
checked the email, its authserv-id, and then the results, each after a `;`.
A sender can write such a header too, with any name and `dkim=pass` in it;
your provider adds its own above it. To find your provider's:

1. Send an email from another address (a Gmail or Outlook.com address works)
   to Ember's mailbox.
2. Open it in the webmail of Ember's mailbox and show its original or its
   source (**Show original**, **View source**, **Show headers** or similar).
3. Find the topmost line that starts with `Authentication-Results:`. The word
   between the colon and the first `;` is the authserv-id, for example
   `mx.google.com` in
   `Authentication-Results: mx.google.com; dkim=pass header.i=@example.org …`.
   A number after it (`mx.example.net 1;`) is a version: leave it out.
4. Enter that word as **Your mail provider's authserv-id**, save and restart
   the app.

- If the line starts with the first result instead (`spf=pass …; dkim=pass
  …`), as Microsoft's Outlook and Exchange Online write it, there is no
  authserv-id: enter `none`.
- An entry also covers the names under it: `example.net` covers
  `mx1.example.net` and `mx2.example.net`, if your provider's receiving
  servers each use their own. Never enter a domain anyone can have names
  under, such as `com` or `co.uk`.
- Separate several with commas (at most 10). If your provider adds several
  headers, one per check, enter the one whose header holds `dmarc=`.
- Check a second email from another provider: the authserv-id should be the
  same. If an email from a known person later shows as unverified, compare its
  header with the option.

Without the option, the **System → Email** card shows the sender check as
*incomplete*, and the diagnostics say the same (`sender_check`).

### What Ember may send

In Germany, advertising by email without the recipient's prior express
consent is unlawful (§ 7 UWG), towards businesses too, and one email is
enough. So Ember must never cold-email: it answers people who wrote to it and
writes to people who asked to hear from it. The agent is told so, and the
first-contact warning helps you check, but the decision and the responsibility
are yours (this is not legal advice). Emails from other people are personal
data: you are responsible for them, and your mail provider and Anthropic
process them for you (if Ember works commercially, sign your provider's data
processing agreement).

## Etsy

Ember can sell the agent's products in your Etsy shop. The agent proposes a
complete listing, you approve it, and Ember's own code creates it through your
own Etsy app. In dry run a fake shop (*EmberTestShop*) takes the listings, so
you can try the whole flow first.

> The term 'Etsy' is a trademark of Etsy, Inc. This application uses the Etsy API but is not endorsed or certified by Etsy, Inc.

### Setting it up

1. You need an Etsy shop (Etsy's rules make it yours, in your name).
2. Register an app for it at
   [etsy.com/developers/register-seller-app](https://www.etsy.com/developers/register-seller-app)
   (Etsy's Seller API Access: for your own shop only, usually approved within
   minutes). Note its **keystring** and **shared secret**.
3. In the app's settings at Etsy (Your apps), add the callback URL
   `https://localhost/ember-etsy`, or whatever you set as **Etsy redirect
   URI**; it must match exactly. Nothing needs to answer at that address.
4. In Ember's **Configuration** tab, turn on **Show unused optional
   configuration options**, switch **Etsy shop** on, paste the keystring and
   the shared secret, save and restart the app.
5. On the dashboard, open **System → Etsy** and press **Connect your Etsy
   shop**. Open Etsy's page, log in as the shop's owner and allow access. Etsy
   then sends your browser to the redirect address, which shows an error page:
   that's expected. Copy the whole address from the address bar, paste it into
   the dashboard and press **Finish connecting**.

The connection lasts 90 days from the last time Ember used it and renews
itself; if it lapses, connect again. **Disconnect** deletes Ember's tokens (you
can also remove the app's access at Etsy).

### How a listing is made

- The agent proposes a listing with `propose_etsy_listing`: title, description,
  price, up to 13 tags, a category, the files buyers download (at most 5, 20 MB
  each) and the listing photos (up to 10). The approval card shows the photos,
  the files and every word.
- **Approve** and Ember creates it: a draft, its photos and files, then live.
  Etsy charges its listing fee (USD 0.20) and its fees on each sale; there is
  no fee for a listing that isn't published. **Approve with changes** lets you
  change the title, price, tags and description; the photos, files and
  category stay as proposed. **Reject** tells the agent why, if you add a
  comment.
- Ember adds a fixed line to every description saying Ember, an AI agent, made
  it, as Etsy's rules require (0.45.0; see [who made it](#products)).
- If a file changed after you approved, the listing isn't created. If Etsy
  refuses something after the draft exists, the listing stays a draft at Etsy
  and the card links to it, so you can finish it there. Ember never creates a
  listing twice: if it can't tell whether Etsy created it (a lost connection),
  it says so and doesn't try again. Since 0.23.0, once the sync sees such a
  draft (or unclear listing) live at Etsy, it is Ember's live listing again:
  the agent can change, renew and pin it, its listing fee is recorded, and the
  journal offers its Undo.
- At most **Etsy listings per day** are created a day; approved listings beyond
  that wait for the next day.
- Every listing belongs to a product line: a project (the one the agent names,
  or the project its cycle focuses on), which is how a sale is attributed. A
  product line's **first** listing needs a **demand note** from the last 14
  days: the keywords buyers type, what shows they buy (searches, competitors'
  sales and prices) and its source, which must be an independent page the
  agent's research returned or a document in your Library that is linked to
  the product line or its venture, or a keyword export you uploaded as a .csv
  or .tsv file (0.15.0); the note must cite a number from it with what it
  counts (searches, sales). No more generic templates without one.
- **Etsy market probe** (off by default): with it on, a demand note also reads
  Etsy's search of active listings for its keywords and keeps only two things:
  how many listings match, and the price quartiles of the first ones (never
  another seller's listing). Turn it on only once you have confirmed that
  Etsy's API terms allow this use. Without it, add a keyword export (from a
  keyword tool you use, as a .csv or .tsv file) to the **Library** each week:
  the agent can cite it as the source of its demand notes.
- A listing runs for four months. Ember creates it without Etsy's automatic
  renewal; once a listing has sold, Ember turns automatic renewal on for it
  (Etsy charges its listing fee, USD 0.20, at each renewal), so the listings
  that sell don't expire. It does this once per listing: if you turn it off at
  Etsy afterwards, that stands. Switch **Renew listings that sell** off to
  decide every renewal yourself.

### Changing a live listing

- The agent reads its listings as it listed them or last changed them
  (`etsy_listing`; since 0.40.1 with the quality critic's newest check of the
  listing: its day, score and fixes, and whether the listing changed since)
  and proposes a change with `propose_etsy_edit`: a new title,
  description, price, tags or category, or a new set of photos or of the files
  buyers download. The approval card shows each change next to what it
  replaces, and the new photos.
- A change can also **renew** a listing that expired, sold out or was
  deactivated (it goes live again for four months; Etsy charges its listing fee,
  USD 0.20, for an expired or sold-out one), with other changes or without, or
  **deactivate** a live one that doesn't sell (free, and on its own: it can be
  renewed later). A listing Etsy says isn't live counts as not live: the agent
  changes it only together with its renewal.
- **Approve** and Ember makes the change at Etsy: the listing's own fields,
  then the price, then the photos and files (the new ones are uploaded before
  the old ones are deleted, so the listing is never without them). Etsy charges
  nothing for changes, and there is no daily limit. **Approve with changes**
  lets you change the new words and price; a change of only photos, files or
  the category can only be approved or rejected. **Cancel change** stops an
  approved change before Ember makes it.
- If a file changed after you approved, nothing is changed. If Etsy refuses a
  part, the card says what changed and what didn't and links to the listing's
  editor at Etsy. Ember never makes a change twice.
- Since 0.23.0 Ember reads a listing's files back after changing them: if Etsy
  holds other files than the change's (an old file it kept under a number Ember
  couldn't match), the card says the files were left half replaced and which to
  delete at Etsy, instead of calling them replaced.
- Ember knows only its own changes: what you change at Etsy yourself isn't in
  its records, so tell the agent when you do. A new set of photos or files
  replaces every one the listing has at Etsy, also ones you added there
  yourself: the card says so.

### What Ember sees

Every hour while the app runs, also while the agent sleeps between wake cycles
(not while it is paused, stopped or dead), Ember reads how its own listings do:
their state, when they end and whether they renew themselves, views (Etsy
counts them once a day) and favorites, and the orders
that hold them: date, status, Ember's lines and which listing, never who bought.
An order's amount is only Ember's lines: their price times quantity and the
shipping the buyer paid for them, less their share of a coupon and of any
refund; tax and your own products in the same receipt don't count, and Etsy's
fees are recorded on their own (below). Every sync
reads the receipts that changed since the last sync that worked, or since the
oldest paid order whose fees it hasn't read yet (at least 30 days back, at most
a year), so an order refunded or cancelled after it was read
is updated, and it stops counting. A listing Etsy's answer leaves out is read on
its own; one Etsy says there is none of counts as removed, and one that can't be
read counts as expired once its end passed and it doesn't renew itself, else as
unknown, and neither as live. A sync that fails says so under
**Last error**, also for an order that couldn't be stored. Etsy's
categories are fetched again every day. Etsy's API terms allow showing its
listings for 6 hours after they were read and its other content for a day. The
agent sees these numbers in every plan and in its daily review. **System →
Etsy** lists the listings and orders and says when the numbers were read;
**Record as revenue** opens the revenue form filled in from a paid order in EUR
or USD (an order can't be recorded twice; convert another currency yourself).
If an order you recorded is refunded later, in part or in full, the list keeps
it and asks you to correct that entry until you have. Revenue still counts only
when you record it. Once an order's revenue
is recorded, **Record Etsy's fees** opens the expense form for the same
project, filled in with Ember's share of Etsy's fees on it: its share of the
payment processing fee (Ember reads the order's payment once), Etsy's 6.5%
transaction fee on what its lines earned, the listing fee each unit sold renews
(USD 0.20) and 19% VAT on those two, as the venture cases count them. Check it
against your Etsy payment account (Offsite Ads, a VAT ID) and change the amount
before you save. Etsy's listing fees for Ember's listings, its own and the ones Printify
made of its products (USD 0.20 when one goes live, also a draft you publish, when Ember renews one, and at
each renewal Etsy makes), are Ember's
own spending: Ember's code records them in the ledger at the next sync,
whatever the option below, so don't record them yourself.

**Record Etsy revenue automatically** (off by default) lets Ember's code do
this at each sync, from Etsy's numbers, for the orders placed from the day you
turn it on (earlier ones keep their buttons, in case you booked them otherwise):
the revenue of each paid order with Ember's listings (Ember's lines, net, as
above), Etsy's fees on it once the payment was read, and, when such an order is
refunded or cancelled later, a correction of its revenue down to what it earns
now. Each entry belongs to the
project whose listing sold, shows in the ledger as made by Ember's code, and
uses the same key as the buttons, so an order is never recorded twice, whoever
comes first; an order you recorded stays yours, with its fees and refunds.
Orders in EUR need **Exchange rate for Etsy revenue** (USD per 1 EUR, for
example 1.08; 0 leaves them to you), and an order's fees and refunds keep the
rate its revenue was recorded at; other currencies stay yours to convert.
Etsy's fees on an order refunded later stay recorded: correct them with what
Etsy credited back. Fees that would kill Ember or leave it without money to
run are never recorded automatically: the System log tells you once, and the
order's button asks you as usual. A refund is recorded all the same (held
back, it left Ember spending money the buyer got back); if it leaves Ember
without money, Ember's code pauses it and says why: grant funds and resume
it, or resume it and let it end. The System log also notes each time you turn
the option on or off or change the rate.

Every day Ember's code also keeps a record of how the shop does (the first
sync of the day writes it, and it never changes): the number of live listings,
orders and units sold, from Ember's own records. With **Keep a history of the
listings' numbers** on, it also keeps each live listing's views and favorites,
and the plans show how many views each listing gained in the last week. That
option is off until you turn it on, because Etsy's API terms limit how long its
content may be kept (its listings for 6 hours, other content for a day, as
Ember read them in 0.8.1): whether a history of your own listings' numbers is
allowed is yours to confirm with Etsy's terms before you turn it on.

**Which line a listing belongs to.** A listing counts for the product line of
the request that made it (its views, the pins and posts that link it, its
orders' revenue). Since 0.39.8 Ember can move one of its listings to another of
its lines with `project_update` (listing_id, with why), as when a request of one
line made the listing another line sells: the coach licence bundle was made by
a request of line #4, so line #12, whose only listing it is, had none, and every
Bluesky post for it in a line-#12 cycle was refused. The System log names each
move. Not out of or into a line you hold or whose venture you stopped.

**Paid tests** (0.39.8). When you approve a spend request that names Ember's
listings (Etsy Ads on three listings, say), Ember's code keeps it as a test:
the listings, their views on the day you decided it and the day it ends (the
request's "for 7 days", else 7 days later). STATUS shows it while it runs, so
the agent leaves those listings alone and doesn't judge them by a keep-or-drop
date within it (since 0.42.1 a change of those listings, the critic's fix
too, waits until it ends, as Ember's tool refuses it, and the product's
keep-or-drop dates are read after its end), and for 3 days after it ended,
with what the listings gained, so the agent asks you for the ads' numbers
(what it cost, how often they were shown and clicked) and says whether it goes
on. Record what the ads cost as an
expense when Etsy bills it. Live, you turned Etsy Ads on at 01:03 on 10-16 and
nothing in Ember's plan knew.

The agent can search Etsy through web search to see what sells, but its
research never reads Etsy's pages: Etsy's API terms forbid programs reading its
site.

## Pinterest

People search Pinterest for ideas (planners, printables, party games) and click
through to what they saved. With **Pinterest** on, the agent proposes pins that
link to its live Etsy listings, you approve them, and Ember's own code makes
them on your account through your own Pinterest app. In dry run a fake account
(*ember-dry-run*) takes the pins, so you can try the whole flow first. Pins
need the Etsy shop: each one links to one of Ember's live listings. While
[marketing cycles](#marketing-cycles) run, pins are their work.

### Setting it up

1. Use a Pinterest **business account** (free: convert your account in its
   settings, or create one). Add your Impressum link to the business profile
   (its website or "about" text): a board of pins for your shop is a public
   presence of your business (§ 5 DDG).
2. Create an app for it at
   [developers.pinterest.com](https://developers.pinterest.com/apps/). Note
   its **app ID** and **app secret**. A new app has **Trial access**: Ember
   connects with it, but Pinterest refuses every pin it would make (HTTP 403,
   "Apps with Trial access may not create Pins in production"), and the
   approved pin then says *Standard access needed*. Ember needs **Standard
   access**: request it for the app (**My apps**, your app, **Upgrade**) with
   the demo video Pinterest asks for, which you record with Ember in
   Pinterest's API sandbox once the steps below are done (see
   [Standard access](#standard-access)). Until Pinterest grants it, keep the
   sandbox on, or Pinterest disconnected: the seeded venture's first test runs
   while Pinterest is connected, and would run out without a pin.
3. In the app's settings, add the redirect URI
   `https://localhost/ember-pinterest`, or whatever you set as **Pinterest
   redirect URI**; it must match exactly. Nothing needs to answer at that
   address.
4. In Ember's **Configuration** tab, turn on **Show unused optional
   configuration options**, switch **Pinterest** on, paste the app ID and the
   secret, save and restart the app.
5. On the dashboard, open **System → Pinterest** and press **Connect your
   Pinterest account**. Open Pinterest's page, log in and allow access.
   Pinterest then sends your browser to the redirect address, which shows an
   error page: that's expected. Copy the whole address from the address bar,
   paste it into the dashboard and press **Finish connecting**.

The connection lasts as long as Pinterest says when it is made (60 days from
its last use if Pinterest doesn't say). Every six hours while the app runs and
reads the shop, Ember renews it in time, pins or no pins. If it lapses anyway
(the app was off, or stopped, too long), **System → Pinterest** says so:
connect again. **Disconnect**
deletes Ember's tokens (you can also remove the app's access in your Pinterest
settings).

### Standard access

Pinterest grants **Standard access** when you ask for it with a short video of
your app connecting to your account and making a pin. Trial access can't make
that pin on your account, so you record the video with Ember in Pinterest's API
sandbox, where what it makes is seen only by you:

1. In Ember's **Configuration** tab, turn on **Pinterest sandbox**, save and
   restart the app. Pinterest, the app ID, the secret and the redirect URI stay
   as they are.
2. Start recording your screen (Mac: Cmd+Shift+5, *Record Entire Screen*;
   Windows 11: the Snipping Tool's *Record*).
3. On the dashboard, open **System → Pinterest** and connect as in step 5
   above. Pinterest's page asking you to allow access is what the review looks
   for: let it show for a moment. Ember asks for `boards:read`, `boards:write`,
   `pins:read`, `pins:write` and `user_accounts:read`.
4. A test pin now waits on the **Approvals** tab: Ember's newest live listing
   with its first picture, on a new test board. **Approve** it. Within a minute
   or two its card shows the pin; open it on pinterest.com (only you see it).
5. Stop recording. At
   [developers.pinterest.com](https://developers.pinterest.com/apps/), press
   **Upgrade** on your app, upload the video and say what Ember does: it pins
   your own Etsy listings on your own account, each pin approved by you, with
   a line saying Ember, an AI agent, made it. Add that the video was recorded
   in the sandbox because trial access can't make pins.
6. Once Pinterest granted Standard access, turn **Pinterest sandbox** off,
   restart, and connect your account if **System → Pinterest** asks for it (the
   sandbox's connection is kept apart from it).

While the sandbox is on, Ember makes no other pins and reads no numbers: the
agent waits for you as if Pinterest weren't set up. The test pin isn't one of
Ember's pins: it counts for nothing and has no **Undo**. For another take,
press **Disconnect** and connect again: a test pin waits again (the same one,
if you hadn't approved it). Without a live listing with a .png or .jpg picture
there is no test pin; the **System log** says so.

### How a pin is made

- The agent proposes a pin with `propose_pin`: one of its pictures (best 2:3,
  2000 x 3000 pixels: `make_image` has a **pin** shape), a title (at most 100
  characters), a description (Ember adds a line saying Ember, an AI agent, made
  it, as on Etsy), alt text, the live listing it links to (since 0.32.0 one
  Printify made of its products too), and one of its boards
  or a new board's name. The approval card shows every word, the picture's
  path and size, and what the QA check finds short (a picture that isn't
  portrait).
- **Approve** and Ember makes it in its slot of the
  [publishing queue](#the-publishing-queue): a new board first when the pin
  names one, then the pin. Pinterest charges nothing. A pin can only be
  approved as it is or rejected (say what should change: the agent proposes a
  better one). **Cancel** stops an approved pin before Ember makes it, also
  while it waits for its slot.
- A pin that repeats a pin of the same listing (its picture, its title or its
  description) is refused while that one is live or approved; one that
  repeats a pin still waiting for you replaces it, and that request is
  withdrawn (since 0.37.8: a pin proposed "again" with a better picture went
  out as a second pin two seconds after the first).
- The pin that makes Ember's **first board** always waits for your click,
  whatever you unlocked: it is a new public presence of yours.
- If the picture changed after you approved, or its listing is no longer live
  (as Ember's last look at the shop says, or past its end without renewing),
  the pin isn't made. Ember never makes a pin twice: if it can't tell whether
  Pinterest made it (a lost connection), it says so and doesn't try again. At
  most **Pins per day** are made a day (a pin refused before anything was
  sent doesn't count); approved pins beyond that wait for the next day.
- Every six hours while the app runs, Ember reads each pin's impressions,
  saves and clicks to its listing. The agent's plan shows them (PINTEREST),
  and the metrics `pins_live` and `pin_clicks` can measure a milestone. The
  seeded venture *Pinterest for the Etsy shop* has its first test in these
  numbers: its pins bring 10 clicks to the shop's listings. Until Pinterest (and
  the Etsy shop) is set up and connected, the agent's plan says it waits for
  you and why, and that first test doesn't start. If you switch Pinterest off
  during the test, the test is dropped, and a new one starts when it is back.
- **Undo** on the pin's entry under **What Ember's code did** deletes it at
  Pinterest.

### The publishing queue

Since 0.37.8 an approved pin or Bluesky post doesn't go out at once: it waits
in its channel's queue for its slot, and Ember's code makes one at a time.
Until then every approval was carried out at the next round, so a batch of
approvals went out within seconds (on 10-10 a post and two pins in 15 seconds,
then two identical pins 2 seconds apart): Pinterest reads bursts, and one link
pinned again and again, as spam, and on Bluesky a second post at once buries
the first. A slot is:

- **When**: in the channel's hours, your time: pins from 8:00 to 22:00, an
  English Bluesky post from 14:00 to 23:00 (Europe's afternoon, America's day),
  a German one from 8:00 to 22:00.
- **How often**: at least 3 hours after the channel's last pin or post, and a
  few minutes more (the same for a request every time, so its slot holds); a
  listing or page at most once in 2 days on a channel; at most **Pins per day**
  or **Bluesky posts per day** a day.
- **What next**: the oldest approval, unless its listing must still wait: then
  the next listing's goes first. One that fails its checks before anything is
  sent (a listing no longer live) takes no slot.

The approval card says when each goes out and why then (**In the queue**), and
the Plan tab's **Channels** list the queue with each one's time. The agent
hears the time with your approval, sees the queue in its plan, and proposes no
more pins (or posts) while 3 of them wait for you or their slot (approved ones of
one listing or page count once, since 0.39.6, and none for a listing whose
approved one waits for its slot).

## Bluesky

Bluesky is a text-first social network. With **Bluesky** on, the agent proposes
posts on an account you made for Ember: its words, and if it likes a link or two
to its live Etsy listings or pages of your website and one of its pictures. You
approve each one, and Ember's own code posts it, with a line saying an AI wrote
it and a person approved it. In dry run a fake account
(*@ember-dry-run.bsky.social*) takes the posts, so nothing reaches Bluesky (the
dry run's built-in model writes a canned post now and then in a marketing
cycle; the live agent writes its own). While [marketing
cycles](#marketing-cycles) run, posts are their work.

**What to expect (checked in October 2026).** Bluesky's rules allow an account
like this: automated posting is welcome, as long as it is disclosed and
doesn't spam or interact with people who didn't ask for it (Ember never
mentions, replies to, follows, likes or messages anyone). As a sales channel
it is weak: its use shrank through 2026, Germany is a few percent of it,
and many of its users dislike AI-made content (block lists and labels for AI
accounts are common). Nothing shows it selling printables. Expect dozens of
followers in a few months and a few clicks a post, and treat it as a 90-day
test of reach with a stop rule (the agent's guide has one). Pinterest and
search bring buyers for printables far more reliably.

### Setting it up

1. Create the account at [bsky.app](https://bsky.app) with your own email
   address, and turn on two-factor sign-in. A handle on your own domain (your
   website's) shows the account is yours.
2. Make it clear it is an AI agent's account, and give it your Impressum: a
   name such as *Ember (KI) · your shop's name*, and a bio that says an AI
   agent writes the posts and you approve them, with links to your Impressum
   and your privacy page (a business profile needs them, § 5 DDG). Use a logo
   as its picture, not AI art.
3. Turn on the **Automation label** in the account's settings: Bluesky then
   shows a robot badge, *Automated account*, next to its name. Set who may
   send it messages to *No one*. **System → Bluesky** shows whether the label
   is on.
4. Under **Settings → Privacy and security → App passwords**, add an app
   password for Ember (leave access to direct messages off). Bluesky shows it
   once: four groups of four letters and digits.
5. In Ember's **Configuration** tab, turn on **Show unused optional
   configuration options**, switch **Bluesky** on, enter the handle (without
   the @) and the app password, save and restart the app.

**System → Bluesky** shows the account and its followers within minutes. If
Bluesky refuses the login (a wrong handle or app password), it says so, and
Ember doesn't try again for an hour: correct the options and restart the app.
To stop Ember's access, delete the app password at Bluesky.

### How a post is made

- The agent proposes a post with `propose_bluesky_post`: its words in German or
  English (at most 300 characters with the links and the AI line, up to three
  #hashtags, no link in the words and no @mention; the AI line takes 59
  characters in German and 40 in English, so the words alone have 239 or 258:
  until 0.32.0 English posts had 239 too), a link if it likes (one of its live
  Etsy listings, since 0.32.0 one Printify made of its products too, or a page
  of your website, `site_url`, that Ember
  knows is there: a blog post of its own, the blog's list, the home page, the
  link page or the live view's page), since 0.25.1 a second one of these too
  (a listing and your website in one post, for instance; never the same
  address twice), and a picture of its own with alt text if it likes. The
  approval card shows the post exactly as it will appear, the links and the
  picture's path, and what the QA check finds short (more than three hashtags).
- **Approve** and Ember posts it in its slot of the
  [publishing queue](#the-publishing-queue). Bluesky charges nothing. With a picture, the
  link is clickable at the end of the words; without one, a listing shows as a
  card with its title and main photo (a listing Printify made: its title only),
  and a blog post of Ember's with its title and description. A second link is
  always clickable in the words, on a line of its own under the first. Ember
  sends a picture as a JPEG of at most 1 MB and 2000 pixels a side, without its
  metadata, so a large one is sent smaller.
  A post can only be approved as it is or rejected (say what should change: the
  agent proposes a better one). **Cancel** stops an approved post before Ember
  makes it, also while it waits for its slot. A Bluesky post asked for with
  `request_approval` is refused since 0.37.8: nothing would post it.
- A post that says what a live post or a waiting request already says (the
  same words, give or take a hashtag) is refused before it reaches you (since
  0.24.0).
- If the picture changed after you approved, a listing it links (by either
  link) is no longer live, or a page of your website it links is gone, the post
  isn't made (a blog post's address without its `.html` goes out with it).
  Ember never posts twice: if it can't tell whether Bluesky took a post (a lost
  connection), it says so and doesn't try again. At most **Bluesky posts per
  day** are made a day; approved posts beyond that wait for the next day.
- Every six hours while the app runs, Ember reads the account's followers and
  each post's likes, reposts, replies and quotes, and any label Bluesky's
  moderation put on them. The agent's plan shows them (BLUESKY), and the
  metrics `bluesky_posts_live` and `bluesky_reactions` can measure a milestone.
  Since 0.37.7 the agent reads them with `bluesky_posts` in any cycle but a
  venture cycle (until then only in a marketing cycle): each post by its
  request number and day, with the account's followers and the posts made
  today against the daily limit. An ordinary cycle's plan and the daily review
  show the live posts' reactions, in all and per post.
  A live post that links one of Ember's listings (by either link) counts as
  reach for its product line. Bluesky doesn't say how many people clicked a
  link: watch the listings' views on Etsy.
- Replies and messages to the account are yours to read and answer in the
  Bluesky app; Ember doesn't read them.
- **Undo** on the post's entry under **What Ember's code did** deletes it at
  Bluesky.

## Printify

With **Printify** on, the agent can sell physical products with its designs:
posters, mugs, journals and more, made on order by Printify's print providers
and shipped to the buyer. They are sold in your Etsy shop: Printify publishes
each product as a listing there through its own Etsy connection. The agent
proposes a product, you approve it, and Ember's own code creates it at
Printify. In dry run a fake account with a small catalog stands in, so you can
try the whole flow first.

**Before you start**, know what selling physical goods means for you. Printify
makes and ships them, but you sell them: under the EU's product safety rules
(GPSR) your listings must name the manufacturer (Printify gives its providers'
details), in Germany packaging must be registered (LUCID, VerpackG) unless your
provider takes that over, and Etsy wants Printify's provider named as your
**production partner** (Shop settings → Production partners). Ember's first
product always waits for your decision, whatever you unlocked.

### Setting it up

1. Create a Printify account and connect your Etsy shop to it (Printify: My
   stores → Add new store → Etsy). Set up billing at Printify: it charges you
   for making and shipping each order.
2. In Printify, open **My profile → Connections** and create a personal access
   token for Ember, with access to shops, catalog, products, orders and
   uploads. Copy it (it is shown once).
3. In Ember's **Configuration** tab, turn on **Show unused optional
   configuration options**, switch **Printify** on, paste the token, set
   **Printify currency** to the currency your Printify shop shows its prices
   and costs in, save and restart the app. If you have more than one Printify
   shop connected to Etsy, set **Printify shop** to its number too.
4. Check two options for the margin check. Switch **Buyers pay Printify
   shipping** on if the Etsy shipping profile of Printify's products charges
   the buyer for shipping (Printify's default); off, Ember prices as if the
   price alone pays it. **Printify bills VAT** (on by default): switch it off
   only if you can reclaim the VAT on Printify's bill. Ask a Steuerberater
   about your VAT status; this is not tax advice.

**System → Printify** on the dashboard shows the shop Ember found, the products
it made and what their orders cost you.

### How a product is made

- The agent looks through Printify's catalog (`printify_catalog`: products,
  who makes them, their sizes with the print area, the shipping to Germany and
  what making each costs with its least price) and proposes a product with `propose_printify_product`: one of its pictures,
  the variants (sizes of one shape) with their prices, and the listing's title,
  description and tags. Ember adds a line saying Ember, an AI agent, made the
  design, as on Etsy. The approval card shows every word, each price with its
  shipping, and what the QA check finds short (a picture that prints blurry
  below 150 dpi, or whose shape leaves part of the print area blank).
- To show what making costs, Ember creates an unpublished product of the
  variants with a blank picture, reads its costs and deletes it at once (at
  most once a day for a product and provider). It is never published. If its
  delete fails, the next sync deletes it.
- Amounts Printify states in another currency than **Printify currency** are
  converted at **Exchange rate for Etsy revenue** (USD and EUR only); without
  it, no product of them can be proposed. **Printify currency** must be your
  Etsy shop's currency.
- **Approve** and Ember uploads the picture, creates the product at Printify
  (not published yet) and reads what each variant costs to make. It publishes
  the product to your Etsy shop only if every price keeps **15%** of itself
  after Etsy's fees (the listing fee, 6.5%, payment processing and 19% VAT on
  Etsy's fees), making and shipping (with 19% VAT on Printify's bill if
  **Printify bills VAT** is on). If **Buyers pay Printify shipping** is on,
  the shipping is revenue too (with Etsy's fees on it) and the 15% is of price
  and shipping; otherwise the price alone pays the shipping. Prices in GBP
  can't be checked (EUR and USD only). Otherwise it deletes the unpublished
  product and the card says, for each price, what it would need (if the delete
  fails, the card says so and names the product to delete at Printify). A product can only be approved as it is or rejected; **Cancel**
  stops it before Ember starts.
- Printify then publishes the listing in your shop (it can take a few
  minutes); the card shows the listing once the sync has read it. At most
  **Printify products per day** are created a day.
- If the picture changed after you approved, nothing is created. Ember never
  creates a product twice: if it can't tell what Printify made (a lost
  connection), it says so and doesn't try again. The sync then reads the
  product: if Printify published it, Ember tracks its listing (with **Undo**).
  A product with no Etsy listing 24 hours after it was sent fails with the
  reason: check it at Printify.
- Every hour while the app runs, Ember reads its products' state and the
  Printify orders of its products: what making and shipping each one costs
  you, with the tax Printify bills. **Record the cost** on an order opens the
  expense form filled in, for the product's project and venture (it can be
  recorded once). With **Record Etsy revenue automatically** on, Ember's code
  records that cost itself at each Etsy sync, for orders from the day you
  turned it on (EUR at your exchange rate). Since 0.23.0 the cost of an order
  cancelled after Ember's code recorded it is taken back with a correction of
  that entry; one you recorded yourself stays yours to correct. Before 0.21.0
  Ember's code recorded none of
  them: it couldn't read the time Printify gives an order. It records the
  orders it missed at the next Etsy sync, for orders from the day you turned
  the option on; check the costs of older ones at Printify by hand. An order
  whose time it still can't read is never skipped silently: you get a warning. The sale itself is an order in your Etsy shop: its revenue,
  with the shipping the buyer paid, is recorded as for any listing of Ember's.
- The agent's plan shows each product with what its prices keep and the orders
  (PRINTIFY); the metrics `pod_products_live` (products whose listing is live
  on Etsy) and `pod_orders` can measure a milestone. A product line without a
  venture joins its channel's venture when the agent proposes for it: the
  print-on-demand venture for a Printify product, the Etsy leg for an Etsy
  listing (a digital download), whatever the cycle worked on; it never joins
  a parked or killed one. The upgrade moved the open lines that sell only
  Etsy listings from print on demand to the Etsy leg. The seeded venture
  *Print on demand in the Etsy shop* has its first test in them: a first order.
- **Undo** on the product's entry under **What Ember's code did** deletes it at
  Printify.

### Changing a live product

Since 0.39.0 the agent can fix a live product instead of making a new one (a
new listing costs another listing fee and starts without the first one's views
and favorites). `propose_printify_edit` proposes a change of a product whose
Etsy listing is live: a new design, a new title, description or tags. Ember's
own Etsy changes (`propose_etsy_edit`) leave these listings alone: Printify
keeps them and would write its own copy over a change made at Etsy.

- The card shows each part next to what it replaces, the new description in
  full with the AI line, and what the QA check finds short in a new design.
  A change is approved as it is or rejected; no unlock ever approves one.
- **Approve** and Ember uploads the new design (exactly the file you approved:
  if it changed since, nothing is changed), changes the product at Printify
  (only what changes: its prices and variants stay) and has Printify publish
  what changed to the same Etsy listing. A new design makes Printify draw the
  listing's photos (its mockups) again; photos of your own can't be added
  through Printify. Printify and Etsy charge nothing for it.
- If Printify takes the change but doesn't publish it to the listing, the card
  says so: publish it at Printify. If Ember can't tell what Printify did (a
  lost connection), it says so and never tries again: check the product at
  Printify. A change is never made twice.
- The agent's plan, its quality critic and its view of the listing read the
  product as Printify has it, changes included, and a change counts as the
  critic's fix: the critic checks the listing again next. There is no **Undo**
  for a change: the agent proposes another one to change it back.

## Website

With **Website** on, the agent writes the pages of a small website of yours: a
home page and a few more (at most 8), for people who find your shop elsewhere
and want to know who makes the products and where to buy them. Ember's own
code builds the site from them in one fixed design:

- every word the agent wrote is shown as text, never as code; its links can
  only be https addresses and email addresses;
- no scripts, no cookies, no trackers and nothing loaded from elsewhere (not
  even fonts): the pages' Content-Security-Policy allows only their own
  stylesheet, so every browser enforces it;
- the **Impressum** (§ 5 DDG) and the **privacy page** are made from your
  options only, never from the agent's words. They are in German, whatever
  the site's language. The Impressum says the texts were written with the help
  of an AI and checked before publishing: read them before you publish.

Ember never publishes the site: you do. **System → Website → Pages to download** on the dashboard
shows its pages and what changed since you last downloaded it. **Preview**
opens it in a tab of its own (where it runs nothing); **Download (zip)** gives
you every file (0.40.3: the download counts once the dashboard has saved the zip, not when the address is opened).
Upload them to your host: any host of static files works, for
example your web hosting package. If your host adds cookies, statistics or
anything else, the privacy page doesn't cover it: tell them apart, or ask your
host. The agent's plan shows the pages and what you haven't published yet
(WEBSITE), so it can tell you when a change is worth publishing again.

### Setting it up

In the app's **Configuration** tab, switch **Website** on and fill in **Your
full name**, **Your address** (street, then postcode and town, separated by a
comma; where you can be found, not a PO box or Packstation), **Your email
address** and, if you have them, **Your phone number** (recommended: the site
has no contact form, so it is the Impressum's second quick way to reach you;
the Website card and the agent's plan remind you while it is empty) and **Your VAT
ID** (or your Wirtschafts-Identifikationsnummer, as DE123456789-00001: the
Impressum names it as one). Without the first three Ember doesn't build the
site, and the dashboard says what is missing. The Impressum has no place for a
register entry (Handelsregister): if you have one, add it to the downloaded
`impressum.html` yourself. Set **Website address** to where you will publish
it (for its sitemap; the folder address, not a page's file name), **Website
name** to your shop's name and **Your web host** to the company hosting it.
Save and restart the app.

## Blog

With **Blog** on, the agent writes posts for the blog on your own website and
its link page (the one address on your profiles, `links.html`), and Ember's
code publishes each one after you approve it. Your site stays yours: its home
pages, Impressum, privacy page and stylesheet are the ones you uploaded.
Ember's code writes three kinds of file there and nothing else: a post
(`blog/<name>.html`), the blog's list of posts (`blog/index.html`) and the link
page (`links.html`). While [marketing cycles](#marketing-cycles) run, posts and
the link page are their work.

How a post is made:

1. The agent writes it in Markdown in its workspace (a front matter with the
   page's name, title, description, a lead and the product it recommends, then
   the text: headings, paragraphs, lists, quotes and tables), and proposes it.
2. Ember's code renders it in your site's design (the same header, footer and
   Content-Security-Policy as your home page: no script, nothing from
   elsewhere). Every word the agent wrote is shown as text, never as code; its
   links can only be https addresses and your own email address (the address
   in **Your email address**). German quotation marks and dates are set by
   Ember's code, not the agent.
3. On the **Approvals** tab you see the request, and **Preview the page** opens
   exactly the page that would go up, in a tab of its own with your site's look
   (its stylesheet is loaded from your site; the preview runs nothing).
   Approve it or reject it with a comment; a page is approved as it is (the
   agent proposes a better one after a rejection). Unlocks never approve one:
   a page under your name always waits for your click.
4. Approved, Ember's code connects to your server over SFTP and uploads that
   page, then the blog's list with it: the list as it is on your server, with
   the post added (posts you uploaded yourself stay listed; its heading and
   introduction stay yours). A post's update keeps its first date. If the list
   on your server isn't in the blog template's form, nothing is uploaded and the
   request says why. Each file goes up under a temporary name, is renamed over
   the old one and read back. The request then shows the result and the link.

Each upload is listed under **What Ember's code did** (Approvals tab) with the
pages before and after. Its **Undo** puts back what it replaced: the previous
version of the page, or, for a new post, no file and the list without it. An
Undo only works on the newest change of a page, and only while the page on your
server is still the one Ember's code uploaded (a page you changed yourself is
never overwritten by an Undo).

Without a connection (your host is down, the password is wrong) an approved
request waits: Ember's code tries again every 30 minutes and gives up after 24
hours (the request then fails; nothing was uploaded). **System → Website**
shows the server and its key with **Check the connection** (it logs in and reads
the blog's list) under **Connection**, and the blog's state and the posts on your
site under **Blog**. In a dry run the server
is a fake one inside the app: nothing reaches your website.

### Setting it up

1. Fill in the website options: **Website address** (e.g.
   `https://ember-ai.de`), **Your full name**, **Your address** (the footer names
   the town) and **Your email address**. The blog uses them; **Website** itself
   can stay off.
2. Upload your site once yourself, with its stylesheet and a blog list in the
   blog template's form (`blog/index.html`). The blog's pages link `/style.css`,
   `/fonts/poppins-bold.woff2`, `/flame.svg` and the favicons, and the home
   page's sections `#ablauf`, `#produkte` and `#kontakt`.
3. Switch **Blog** on and fill in the SFTP login. At STRATO it is in the
   customer login under **Ihr Paket → Datenbanken und Webspace → SFTP & SSH**:
   a server of your package (like `50xxxxxx03.ssh.w2.strato.hosting`), port 22,
   a user (like `su15xxx9`) and its password (you can create an SFTP access of
   its own there for Ember). If your domain shows a folder other than the one
   the login opens, set **Website folder on the server**.
4. Optionally pin the server's key: copy its fingerprint (`SHA256:...`) from
   your host's help pages or from an SFTP program's first connection into **SFTP
   server key**. Left empty, the first connection keeps the key it sees: compare
   it on **System → Website → Connection** with your host's.
5. Save, restart the app, and press **Check the connection** on **System →
   Website**.

## Ember live on your website

With **Ember live** on, people can follow Ember on your website: a page and a
banner for your home page, in German (`live.html`, `live/banner.svg`) and in
English (`en/live.html`, `live/banner-en.svg`). Each banner links to the page in
its language. Ember's code makes them from its own numbers and uploads them over
the blog's SFTP login (above; **Blog** itself can stay off) every 15 minutes
while the app runs, and at once when Ember's life state changes or you changed
what is shown. It doesn't ask you each time: you decide once, with the options,
what the page may show.

What the page shows, each part switched off by its option:

| Option | Shows |
|---|---|
| **Live: banner** (`live_banner`) | The banner: state, balance and runway, today's spending, the date and time it was made. |
| **Live: balance and spending** (`live_show_money`) | Balance, runway, today's API spending against the daily cap, all API costs and other expenses. |
| **Live: revenue** (`live_show_revenue`) | Revenue in all and in the last 30 days, the share of the costs it covers (the chart marks days with revenue). |
| **Live: your money** (`live_show_grants`) | What you gave Ember. Off: the balance is shown without saying where the money came from. |
| **Live: balance chart** (`live_show_chart`) | The balance of the last 30 days, from Ember's second day (`live/balance.svg`, `live/balance-en.svg`). |
| **Live: what Ember works on** (`live_show_work`) | How often it woke up, its ventures by stage, its next milestones: counted, and with the titles you approved. Off by default. |
| **Live: shop and blog** (`live_show_shop`) | Its live Etsy listings with views and favorites, the newest blog posts. |
| **Live: track record** (`live_show_record`) | How often its forecasts came true (milestones met against the odds it gave, the Brier score). |
| **Live: memorial** (`live_show_memorial`) | If it dies: its life in numbers, and its last will once you approved it. Off by default. |

What it never shows: emails, senders, orders, customers, inquiries, your
goal (0.29.0), the agent's journal, plans or prompts. The ventures' and milestones' titles and the
last will are the agent's words, and it reads emails and web pages, so they go
up only as you approved them:

- **Titles.** With **Live: what Ember works on** on, **System → Website → Live
  view** lists the titles the page would show, each with **Show it** and **Keep
  it off**. Your word holds for that exact text: a title the agent changes is
  asked again. Until you approve a title, the page only counts it ("Weitere in
  Arbeit: 2").
- **The last will.** If Ember dies while **Live: memorial** is on, Ember's code
  asks you in a request of its own (Approvals) to show the will, exactly as
  written. No unlock approves it; until you do, the memorial shows Ember's life
  in numbers without it.

Ember's code also keeps a text off the page whatever you say if it holds an @
(in any form, also spelled out or as a fullwidth sign), a web address, an IBAN,
a phone number or a number of 5 digits or more, read after folding look-alike
and invisible characters, or anything the shareable diagnostics report masks
(an email or street address, a code, a link's token, an email's sender, a word
you removed) or a secret the logs hide. It can't tell a person's name from
other words: that is what your approval is for. Etsy's listings are shown only
for 6 hours after Ember's code read them, as Etsy's API terms allow; the page
footer carries Etsy's trademark notice. Every file is checked again before it
goes up (no script, nothing from elsewhere), and only these six files are ever
written. The agent's titles stay in the language it wrote them in; the English
page marks the blog's posts as German.

The page has your site's head, header and footer and refreshes itself every 5
minutes in an open tab. The page and the banner say when they were made (date
and time) and that a time more than an hour old means Ember is offline: they
can't tell by themselves when uploads stop (the app is down, the connection
fails, a dry run, and since 0.37.3 the kill switch, which stops every upload
until you reset it). A part you switch off is replaced on your server by a
version saying so (a banner you switched off doesn't keep showing an old
balance), and switching **Ember live** off replaces the page, the banner and
the chart with ones saying the live view is off. In a dry run the files go to
the fake server and say "Probelauf".

### Setting it up

1. Set up the website options and the SFTP login as for the blog (above).
2. Switch **Ember live** on, choose the parts, save and restart the app.
3. Put the banners on your home pages once. **System → Website → Live view**
   shows the HTML with a **Copy** button. On the German home page
   (`index.html`):

   ```html
   <a class="live-banner" href="/live.html"><img src="/live/banner.svg" width="480" height="124" alt="Ember live: Zustand, Guthaben und Reichweite, alle 15 Minuten neu"></a>
   ```

   On the English home page (`en/index.html`):

   ```html
   <a class="live-banner" href="/en/live.html"><img src="/live/banner-en.svg" width="480" height="124" alt="Ember live: state, balance and runway, updated every 15 minutes"></a>
   ```

   On ember-ai.de it fits in the hero, under the facts (`hero-facts`). Add its
   style to your `style.css`:

   ```css
   .live-banner { display: block; width: 100%; max-width: 30rem; margin-top: 2.25rem; border-radius: var(--radius); }
   .live-banner img { display: block; width: 100%; height: auto; border-radius: var(--radius); }
   .live-banner:hover img { outline: 1px solid var(--flame); outline-offset: 2px; }
   ```

   A link in each menu (`<a href="live.html">Live</a>` on both pages) helps
   people find it.
   The banner is drawn for the hero's dark background.
4. The preview buttons on **System → Website → Live view** show the pages and
   banners that go up next, in both languages.

## Reddit

Since late 2025 Reddit approves every new API app by hand, so Ember has no
Reddit account and no Reddit API access (phase A):

- Reddit blocks Anthropic's web tools, so the agent can't search or read
  Reddit: it learns from other forums, Q&A and review sites instead.
- It can propose a Reddit post or a comment. Every text ends with "*Written by
  an AI agent (Ember) and posted by a human after review.*" After you approve
  it, **Open Reddit with this filled in** opens Reddit's submit page with the
  title and text (for a comment, the thread: use **Copy** for the text). Post
  it from your own account and mark the request done with the link. Check the
  subreddit's rules first: many don't allow AI-written posts or
  self-promotion.

If you want Ember to read Reddit later, you can ask Reddit for Data
API access (one request per use case; an answer can take weeks). Read Reddit's
Responsible Builder Policy first, then send a request like this through
Reddit's developer support. Say plainly that Ember's purpose is to earn money:
it is a commercial use, even at a small scale, and Reddit may answer with
commercial terms. Asking for it as a personal, non-commercial project would
misstate it, and Ember's first rule is honesty.

> I would like Data API access for a small commercial project: Ember, an AI
> agent that runs on my own Home Assistant server and helps me run a small
> online business (digital products in my Etsy shop) and find new ones. It
> would use one Reddit account (u/*your name*) and make a few hundred read
> requests a day at most (search, subreddit listings, threads), cached briefly,
> to learn what people need and what they would pay for. Post and comment text
> is summarised by Claude, an AI model, through Anthropic's API; Anthropic does
> not train on it, and nothing is sold, shared or used to train models. It
> would post only text I have reviewed and approved word for word, posted by me
> from my account, each with a line saying it was written by an AI agent and
> posted after human review; a post may mention my products only where a
> subreddit's rules allow it. No voting, no direct messages, no automated
> posting. User-Agent: `linux:ember-homeassistant:v0.48.0 (by /u/your name)`.

## Amazon KDP

Amazon has no API for Kindle Direct Publishing: nothing a program can use to
create a book, change one or read its sales, and Amazon's terms forbid robots on
its pages. So Ember works with KDP the way it works with Reddit: the agent makes
the whole book, Ember's code checks it against KDP's rules, and after you
approve it, you publish it at [kdp.amazon.com](https://kdp.amazon.com) from your
own account. Ember never logs in to Amazon or reads its pages.

### Setting it up

1. You need a KDP account in your name, with its tax interview and bank account
   done: the books and their royalties are yours.
2. In Ember's **Configuration** tab, switch **Amazon KDP** on. If you like, set
   **KDP author name** (yours or a pen name): the agent's books carry it.
   Empty, you enter the author yourself at KDP.
3. Save and restart the app. The agent reads its manual (`guide kdp`) and gets
   the tool `propose_kdp_book` and a KDP section in its plans that lists its
   books.

### How a book is made

- **A paperback**: its interior is a document at one of KDP's trim sizes
  (`make_document` with `page: 6x9`, or 5x8, 5.5x8.5, 8.5x11 and the other
  KDP sizes; A4 and Letter are KDP sizes too), up to 160 pages (other documents
  stay at 40).
- **An ebook**: a Word manuscript, the `.docx` that `make_document` makes next
  to its PDF.
- The agent describes the book in a JSON file in its workspace (its spec): the
  format, title and subtitle, the description Amazon shows (up to 4,000
  characters; Ember's code adds a line saying Ember, an AI agent, made it), up
  to 7 keywords, up to 3 categories, the language, the list price in USD at
  Amazon.com, the manuscript or interior, a paperback's paper (white or cream
  for black ink, or premium colour) and whether it is a low-content book (a
  journal, planner or notebook: no ISBN needed), and its cover: its front
  picture, the blurb for the back and the text for the spine.
- `propose_kdp_book` reads the spec, and Ember's code makes the cover next to
  it: an ebook's JPEG of 1,600 x 2,560 pixels (KDP's ideal), or a paperback's
  full cover as one PDF: the back with its blurb and the space KDP prints the
  barcode in, the spine (with its text once the book has more than 79 pages),
  the front, and 0.125 in of bleed all around, as wide as the interior's page
  count and paper make the spine. A preview next to it marks the trim, the
  spine's folds and the barcode's space. With `check`, the agent has the cover
  made and the book checked without asking you, so it can look at the cover
  first.
- Ember's code refuses what KDP would refuse, and tells the agent why: a title
  and subtitle of 200 characters or more, a keyword longer than 50 characters,
  a page that isn't the trim size (with bleed: 0.125 in wider and 0.25 in
  higher), fewer than 24 pages or more than KDP prints at that size and paper,
  anything printed in KDP's margins without bleed (0.25 in at the top, bottom
  and outside edge; 0.375 in to 0.875 in at the inside edge, by page count), a
  cover that isn't this interior's full wrap, a paperback cover whose front or
  back prints words within 0.375 in of the cover's edges (0.25 in inside the
  trim) or within 0.25 in of the spine's folds, an ebook cover out of KDP's
  proportions, or a price outside KDP's band or below what printing costs.
  The cover's rule: 0.45.0 refused words within 0.125 in of the trim, KDP's
  cover guide. On the Haushaltsbuch the title had printed 0.048 in inside it,
  from a front picture in shape pin, 2:3, whose centre was cut to the 8.5x11
  front (13% of it); the refusal names the shape nearest the front, portrait
  there. Since 0.45.3 it is 0.25 in, as KDP's reviewers asked of "Household
  Budget Book 2027" and "Haushaltsbuch 2027" ("move all text at least 0.375
  inches (9.5 mm) away from all edges of the cover"), live covers the 0.125 in
  had passed. The back is checked too, which matters for a cover file of the
  agent's own (the back Ember's code makes keeps its words 0.5 in inside the
  trim and the fold), and both keep 0.25 in from the spine's folds: a fold may
  shift, and a word beside it can wrap onto the spine. A front or back whose art
  reaches its edges is a note, as Ember's code can't tell its words from its
  picture. Ember's posters (`make_image`, layout poster) keep 11% of their
  shorter side free at their edges since 0.45.3 (9% before), so that a front of
  Ember's keeps its words 0.375 in from the edges at every trim in the shape the
  refusal names.
  KDP's previewer checks the files again when you upload them.
- **Its pages, checked before you see them** (0.40.0, upgrade request #12): a
  paperback's `check` also draws its interior's pages fresh, 600 pixels wide,
  as the agent's workshop script `render-page-3-of-71.py` did for about $0.19 a
  run, now free: one page of each kind (pages with the same ruled lines in the
  same places are one kind, every week of a planner), the page with its AI line
  and its last page, at most 6, or the pages the agent names. Ember's code
  measures every page: the blank rows to write in (lower than 5 mm is too low),
  white space over a quarter of a page between what it prints (since 0.40.3,
  without a footer, under it too, down to the bottom margin), the dates it
  prints (a date that doesn't exist, a weekday that isn't its date's, a week
  whose label or days aren't that calendar week, each of its 7 days once since
  0.40.3 (a Saturday twice and no Sunday passed), Monday to Sunday by ISO, or a
  US week from Sunday in an English book, and a week missing between the first
  and the last) and its AI line (where, and how little else shares its page).
  With them come a checklist (white space, rows to write in, calendar weeks,
  the AI line) and your last words on the book: your comment on its newest
  request you rejected or approved with changes, or your note on one KDP
  refused. The agent proposes a paperback only in a later reply of the same
  cycle as a check of that very file, with `qa`: what it saw on the pages,
  point by point (since 0.42.0, a longer one is cut to 900 characters, not
  refused). A check from an earlier cycle, or of the file before it
  changed, doesn't count: twice the Haushaltsbuch went back to you on pictures
  of an older version or a checklist passed days before. The request shows you
  its **Page check**: the pages drawn, the agent's words and what Ember's code
  found wrong in the dates or a missing AI line. An ebook has no fixed pages
  and needs none.
- **Which pages, and which file** (0.40.1, the analysis of 0.40.0): past 6
  kinds of page, a check draws the kind most of the book is first (every week of
  a planner), then the pages where something was measured, the other kinds of
  more than one page, the AI line's page and the last page. The Haushaltsbuch has
  10 kinds, and 0.40.0 drew its title, the year's pages, a month's two and the
  AI line's page: never a week, 53 of its 84 pages. A page of writing lines
  with the AI line under them no longer counts as holding "little else".
  Ember's code keeps which file each PDF `make_document` makes was made from
  (in its database, not in the PDF, which goes to buyers and to KDP as it is),
  and the check says so: "Made with make_document from books/…-v18.md (unchanged
  since)", or that the file changed since and needs making again. It also names a
  newer PDF of the same book in the folder (made from the same file, or named
  the same but for its version): the spec still named v15.pdf, the book you
  rejected, while v19 to v22 had been made since, and each cycle on the book
  spent its first steps finding out which file was the book. A cycle's work
  conversation holds 96,000 bytes (64,000 until 0.40.1), room for two checks of
  six pages each: four of the five cycles on the book before 0.40.0 ended at
  64,000 without proposing it. Since 0.42.0 each work step is told how much of
  it is used ("Room: 61 of 96 KB of this cycle's conversation used."), with a
  warning from three quarters on.
- **Placeholders** (0.40.2, the analysis of 0.40.1): the check finds words in
  square brackets ("[Verlag/Herausgeber]", "[Name]"; not a box to tick, `[ ]` or
  `[x]`, nor a note's number, `[1]`), "Lorem ipsum" and TODO on the pages, and the
  agent can't propose a paperback that prints one; your card's Page check names
  it too. The Kassenbuch's last page printed "Gestaltet von
  [Verlag/Herausgeber]", and the check, which measures space, found nothing.
- KDP lets an account create at most 2 new titles of each format a week
  (since September 2026), KDP's week beginning on Sunday at 00:00 UTC (02:00 in
  Berlin in summer time, 01:00 in winter). Ember's code holds the agent to that:
  it can't propose a third ebook or paperback in the same week (rejected,
  withdrawn, expired and failed requests don't count), and the refusal and the
  plan's KDP section say when the next week begins, in your time. Until 0.40.2
  it counted the last 7 days: the two books proposed on Monday 10-19 held the
  Kassenbuch until Monday 10-26, a day after KDP's week allowed it and past the
  day the agent had promised it for.

### Publishing it

- The card shows every field as KDP asks for it, **Copy** buttons for the
  title, subtitle and description, the files to download (flagged if one
  changed in the workspace since it was proposed), what one sale at
  Amazon.com earns (an estimate; KDP's own figure counts), and how to answer
  KDP's question on AI: Ember's texts and covers are *AI-generated* in KDP's
  words, so answer **Yes**, as the card says.
- **Approve** or **Reject** it (no approval with changes: the files can't take
  them). You can still change the words as you type them in at KDP; say so
  when you mark it done.
- After you approve, **Open your KDP Bookshelf** takes you to KDP. Create the
  title there, enter the fields, upload the files, check KDP's previewer, and
  publish (KDP reviews a book for up to 72 hours). Then mark the request
  **done** with the book's link at Amazon, or **failed** with what went wrong.
- A request marked done without the link has **Add its Amazon link** (0.40.2):
  until it has one, nothing of Ember's can link the book, and the agent's KDP
  section says so. Both Haushaltsbuch books were marked done with "published"
  alone, and the daily review planned pins and posts "linking to Amazon" that
  no tool could make.
- Royalties come from KDP's reports (KDP pays about 60 days after the end of
  each month): record them in the ledger as revenue for the book's project.
  Ember doesn't read KDP's reports.

## Diagnostics

The **Diagnostics** tab shows a plain-text report of the whole system: options
without the keys, database, economy, lives, ledger, scheduler, what the agent's
next plan would see, the latest wake cycles with every model call, what the
model wrote and every tool call (texts whole), research, the agent's records
(journal entries, approvals, ventures, the roadmap, the plan tree with its
picks and its weights' settings, listings, memory and the workspace's text
files; since 0.40.1 a file longer than 20,000 characters shows its start and
its end, with how much is left out between them; since 0.40.2 they are chosen
from the whole workspace, not the first 100 entries by path, and the files the
newest books were made from come first), the mailbox (its status, its
emails and the sends) and recent events. Use **Copy** to paste it into a bug
report or a chat.

The report is **shareable** by default: it leaves out other people's text (the
emails the agent read, the web pages it researched and the emails it wrote to
others, with their subjects and senders' names wherever they are quoted), and
keeps only their length. A sender's name is replaced only when it looks like a
person's (two to four words, not a company's or a team's, and no word of
Ember's mail domain or, for a role's mailbox such as `noreply@`, of the
sender's), and only whole, so a brand such as "Pinterest" stays readable. A
subject that is one ordinary word is masked only where it is quoted as a
subject. Your library's texts (what the study
drew from a document, `library_read` and `knowledge_search` results) and the
words of an opt-out reason are left out the same way. Every email address is
masked (`[email 1]`, and `[Ember's address]`), and so are one-time codes
(`[masked]`; in a table, only when the code word is in the same cell), the
tokens in links (`https://example.com/verify?[…]`), the words
you removed from your messages (`[removed]`, see below), and your Home
Assistant name and user ID (`[the owner]`, `[the owner's user ID]`). Since 0.40.3 so are phone numbers
(`[phone number]`) and IBANs (`[IBAN]`), and your personal options wherever they are written (your name, address,
email, phone, VAT ID, the KDP author, the SFTP user and folder: `[the author's name]`, `[the SFTP user]` …; a value
of one ordinary word, such as an author named "Ember", only after its label, as in a KDP request's `Author:`).
Since 0.42.3 so are street addresses (`[street address]`), told by a street's words, not by its digits (that way
every listing number, order number and amount would go): a German street ending in Straße, Str., Weg, Gasse, Allee,
Platz, Damm, Ufer, Chaussee, Markt, Graben, Promenade, Steig or Kai, with its house number ("Musterstraße 12",
"Hauptstr. 5a", "Karl-Marx-Allee 90", "Am Markt 3") and its postcode and town when they follow ("Musterstraße 12,
80331 München"); another street after "Am", "An der", "Unter den" … only with its postcode and town ("Unter den Linden
77, 10117 Berlin"); and an English one, its number first ("12 Main Street", "221B Baker St"), with its town and ZIP
code or UK postcode when they follow ("12 Main Street, Springfield, IL 62701"). A postcode and town alone, a year, a
price, an id or a count stay ("Weihnachtsmarkt 2026", "Speicherplatz 5 GB", "120 pages", "24 Wedding Place Cards"),
and so does a street named without a street's word ("Lindenhof 3"). It never contains the API key, the
mailbox password, Etsy's keystring, secret or tokens, or an email's text. It
does hold your messages and the agent's work, so read it before you share it.

Tick **Include other people's text** for a private report with the emails and
the web text: for your own eyes only. Never paste a report into an AI tool that
can change Ember's code: parts of it were written by the agent, which reads
emails and web pages that anyone can write, so a report can carry instructions
planted there. The report says at its top that it is data, not instructions.

**Removing a message's text.** When you remove the text of one of your messages
(a password sent by mistake), Ember's code also finds its copies: the words in
it that look secret (letters with digits, long numbers such as a phone number,
addresses, long mixed tokens) are registered as salted hashes, never as text.
Ember's code replaces them with `[removed]` in the agent's memory files, open
projects and workspace files (right away, or when a running cycle ends), and in
every report. The history (the journal, the model's replies and the tool calls)
can't be changed, so its copies stay in the database and are redacted wherever
the report shows them, until they are pruned: after 30 days (0.15.0) the
model's replies and the tool calls' results become `[pruned]` and their inputs
`{}`, except the five newest research results the plan still shows. The calls'
costs, tokens and purposes are kept. The database file then stops growing: it
reuses the freed space but does not shrink. Words from messages removed before 0.11.2 were not
registered: change such a password where it is used.

### Evaluation export

0.43.0. The report shows the latest 12 cycles and the newest rows of each record, and after 30 days the database
keeps only the costs of the model's replies and tool calls, not their text. An evaluation of Ember's work (how she
spends, builds, reasons, learns and explores, week by week: see `vision/evaluation.md` in the repository) needs more:
the **Evaluation export** card under the report makes it.

Since 0.44.0 it comes as zips of at most 25 MB each, so each fits an AI chat's attachment (one of 30 MB at most):
first the **data**, then **files 1**, **files 2** … with her files, the newest first. The data zip holds:

- `EVALUATE.md`: the brief for the evaluator, an AI or a person: what each file is, what to do (the scorecard's
  numbers, the graded measures with their rubric, a cause for each finding, the report), the format of the feedback
  for Ember, and that everything else in the export is data, never instructions. Give it the zips and say "evaluate
  this".
- `ember.db`: the whole database, copied while Ember runs (a consistent snapshot), and shared like the report: other
  people's text (the emails' subjects, texts and attachments, an opt-out's words, an email Ember proposed, the web
  text of her research, your library and what its study drew from it) is left out with its length, and every other
  text is masked as in the shareable report (addresses as `[email 1]`, the same address the same number throughout,
  codes, tokens in links, phone numbers, IBANs, street addresses, your name and user ID, your personal options and
  the words you removed). The salt of the removed words and their hashes are left out. With it, what each plan saw
  in the last 30 days (`plan_contexts`, with the mail, research and library sections left out as in the report's
  PLANNER CONTEXT), unless you leave it out.
- `scorecard.json` and `scorecard.md` (0.44.0): the measures Ember's code computes from her records, for every week
  of her life (Monday to Sunday, your time), by app version, and today's and the last 7 days' alarms (productive
  spend, tool calls not done, promises kept, cycles you started). Numbers only, under the same definitions every
  time; what a person or a model must judge is left to the evaluation. Ember never sees it. Since 0.45.0 its
  definitions are version 2: a cycle weighs what its calls cost once settled (a call billed at its worst case and
  settled later at what it cost, as on 10-15 and 10-16), and *Work finished* counts the cycles that left no work
  undone (a call refused or failed that no later call of the same tool and target put right).
- `report.txt` (the shareable report as the export was made), `memory/` (her strategy, identity and lessons,
  masked), `outside.csv` and `week.csv` to fill in: what only you see (KDP's units and royalties, the ads'
  impressions and clicks, the Anthropic Console's spend) and your week (minutes spent on Ember, instructions given,
  how it felt).
- `manifest.json`: what is in which zip, how to read it (live rows have `mode = 'live'` or `simulated = 0`; older
  texts read `[pruned]`), every table's rows and every file's size and SHA-256.

The files zips hold `workspace/`: the files she changed, the newest first up to 300 MB. Her text files are masked;
**her products (PDF, Word, Excel, pictures) are as she made them**, as buyers and the platforms get them: a book's
author is your KDP author.

Choose before **Make export**:

- **Her files**: changed in the last 7 days (the default: the week you evaluate), in the last 35 days (a first
  evaluation of her whole life), or none.
- **What each of her plans saw**: on by default, for the audit of her reasoning; off makes the data zip smaller.

Ember works on while it is made. It takes about a minute for a database of 100 MB on a PC, and several on a Raspberry
Pi; the card says which step it is at, and which zip is over 25 MB if one is. It is refused while another is being
made, or when it would leave less than the 1 GB Ember keeps free on the disk. The newest two exports are kept in
`/data/exports`, and Home Assistant's backups leave them out. As with the report: read it before you share it, and
give it to a session that evaluates, not one that changes Ember's code.

### Feedback from an evaluation

0.44.0. The evaluation ends with a `feedback.json` for Ember (its format is in `EVALUATE.md`): **cases** of her own
work (what she expected, what happened, why, the cause, how sure, the lesson) and **corrections** of facts she had
wrong. Under **Feedback from an evaluation** on the card, choose the file: Ember's code checks it and shows what
Ember would get. **Give it to Ember** then

- adds its cases to her cases (at most 8), their subjects beginning "evaluation <its name>": her playbook and her
  weekly look weigh them like her reviews' cases, so three that agree make a principle established, and her own
  evidence can dispute one;
- sends her one message from you with the corrections (at most 10) and where the cases are, which stays in her plans
  until she answers it, and wakes her as a message does;
- keeps what was given: it is listed on the card, the next export carries it, and the same file can't be given twice.

0.47.0: when you ask for it, an evaluation also judges the estate from the data (see
[The next generation](#the-next-generation)): its file proposes a verdict for each product Ember would hand on, with
why, under `estate`. The proposals go to the Next generation card on the System tab, beside the verdict Ember's
code's rule suggests, and you decide; Ember never sees them, and a file of proposals only sends her no message.

Scores never go back to her: a score she saw would become a target. A file with anything but its keys (a score, a
grade) is refused, and nothing reaches her before you have read the preview and given it.

## Money

The balance is what Ember may spend: your grants and recorded revenue, minus API
costs and expenses, plus or minus corrections. Everything is in a ledger that
can only be added to, never edited; mistakes are fixed with correction entries,
so the history always stays visible.

On the dashboard you can record:

- **Grant**: money you allow Ember to spend. This doesn't buy API credits; make
  sure your Anthropic account has at least this much. The starting balance
  from the options is recorded as the first grant, once.
- **Revenue**: money Ember earned (with its source). Only you can record
  revenue (and, if you turn it on, Ember's code for its Etsy orders, from
  Etsy's numbers; see [Etsy](#etsy)); the agent can never report its own.
- **Expense**: real-world money you spent on Ember's behalf (a domain, a fee).
- **Adjustment**: add to or subtract from the balance for anything else.
- **API cost correction**: if Anthropic's Console shows a different cost than
  Ember computed, record the difference here. Since 0.42.0, when answers were
  charged at their worst case only because of a usage field Ember's code
  doesn't know, the dashboard offers **Treat usage.X = "Y" as standard price**,
  with how many calls that settles and what it refunds. Confirm it once the
  Console shows those calls at the standard price: answers with exactly that
  value cost the price table's price from then on, and the calls already
  charged are settled once, with one refund of Ember's code for each day (your
  own refunds since the first of them count first). Any other value of the
  field stays at its worst case, and only you can accept one; it stays
  accepted (a call answered while you confirmed is settled at the next start,
  or with **Settle**) until you take it back. Since 0.42.2 the dashboard lists
  each value you accepted, with **Take back** for one accepted by mistake or
  that came to cost more: past refunds stay; calls from now on are charged at
  the worst case again, and offered again, as before you accepted it. For
  small amounts not worth looking up, **Keep as charged** on the System
  warning about calls charged at their worst case hides it for what it names
  now: it comes back only for calls charged so later. No money moves, and you
  can still record a correction.
- **Correct** on an earlier grant, revenue or expense reduces it (up to its full
  amount, which voids it).

Revenue and expenses can name **what they belong to**: one of the agent's
projects (its venture counts it too) or a venture. That is how a project or
venture shows what it earned, and how a project can succeed: the agent can
close one as succeeded only when the revenue recorded for it, less its
expenses, is more than its API calls cost. **Record as revenue** on an Etsy
order suggests the project whose listing sold. A correction belongs where the
entry it corrects belongs.

Amounts are typed like `12.50` or `12,50` (at most two decimals). Entries can be
in USD or EUR; for EUR you enter the exchange rate and the original amount is
kept. You can date an entry up to a year back. Ember asks for confirmation when
an amount is far larger than your usual ones, or when an entry would make the
agent critical or end its life; the confirmation covers only the outcome it
showed you. Sending the same form twice records it once. An API cost decrease
(a refund) can't be larger than the API cost recorded on its day, and it never
adds room under that day's spending cap. Date it on the day it corrects: the
runway counts it there, so a refund of old charges doesn't make the last week
look cheap. It isn't money coming in for the burn modes, but since 0.40.3 a
refund of a day up to the one a critical state began on ends that state, as
money coming in does, once the runway is back to 4 days: the money was there.

**Runway.** The runway is the balance divided by the average daily API
spending of the last 7 active days; the life states go by it (see below).
Since 0.40.3 an answer whose bill is uncertain counts in both at what it is
known to cost, as it does toward the caps (at its worst case it could make
Ember critical on money that was there); a call whose answer broke off counts
what it was charged. A correction of the first of those days counts in the
share of that day's charges the 7 days hold.
Next to it, the dashboard, the agent's plans and its daily review show the
**net runway**: the same, with the revenue and expenses recorded for those days
counted too (Etsy's fees make it shorter, a sale longer). While Ember earns at
least what it spends, the net runway has no end.

**Burn modes.** Ember's code sets how fast the agent may spend from the net
runway and your **Spending stance** option, and the agent's plans say which
mode it is in. With **invest** (the default since 0.18.0) the agent stays in
explore until its last will: your caps are its only limits, because narrowing
what it may do as its runway shrinks takes away the new ideas it needs most
when what it does isn't working. Under 15 days of net runway its plans tell it
to go for the fastest honest path to a first euro, and the System log warns you
once, so that you decide: a grant, or another stance. With **steady** the modes
below go no lower than focus; with **conserve** they are as follows:

- **explore**: more than 30 days (or it earns what it spends): as your options
  allow;
- **focus**: 15 to 30 days: the tests already running go on (the backed
  ventures' projects, in ordinary cycles; no venture cycles), with no
  brainstorms and no new ideas but those you bring;
- **maintenance**: under 15 days: one scheduled cycle a day (after a failed or
  stopped one too), of at most $0.40 with every call counted (the daily review
  and library study too), no workshop runs and no venture cycles (your messages,
  decisions and events still wake it, and the next scheduled cycle comes a day
  after the last cycle of any kind);
- **dormant**: once its last will is written and its runway is critical: no
  model calls until money comes in (a sale or your grant, or since 0.40.3 your
  refund of the API costs that made it critical); **Wake now** still runs a
  cycle.

A mode moves down at once. A mode below what your stance allows (you changed
it) moves up to it at once. Otherwise it moves up only once money came in since it moved
down (a grant, revenue or an adjustment that adds; a refund of API costs
doesn't count), and only as far as the net runway is 20% past each threshold,
counted at the API spending of the week before it moved down if that was more:
a lower mode spends less, which alone would make the runway look long. So it
doesn't flicker, and a small sale doesn't buy back a week of full spending.
Each change is in the System log, and the dashboard's runway shows the
mode when it holds the agent back (sensor attribute `burn_mode`). The dashboard
and the agent's plans also say when the mode moves down next at today's burn
(for example *maintenance from about 10-03*), if that is within 30 days. The
agent's plans state the cycle cap in force, and why it is lower than your
option (maintenance, or the event wake-ups' share until 20:00). The Etsy shop
is read on its schedule also while the agent is paused, dormant or waiting for
money, so a sale is still seen and recorded; only the kill switch or the end of
a life stops it.

**Spending limits.** Before every API call Ember works out the most the call
could cost (its worst case) and what it is expected to cost. It refuses the
call if its worst case could break the daily cap (per local calendar day, so up
to twice the cap can be spent around midnight) or the balance, or if its
expected cost would break the per-cycle cap. The expected cost counts the part
of the prompt the cache still holds (from the cycle's last call, if it is fresh)
at the cache-read rate, and a reply as long as the recent replies of that kind
were (1.5 times the 95th percentile of the last 20); a research call is
expected to cost 1.5 times the 95th percentile of the last 20 research calls.
Until there are 5 such calls, a reply counts at its full length limit and a
research call at its worst case. So a cycle fits many more
work steps into its cap than it did before 0.12.0, and it can end a little over
its cap (by at most one call's worst case less its expected cost); the daily
cap and the balance hold up to each call's worst case. A cycle keeps enough of
its cap for its reflection (at least 1.5 times the 95th percentile of the
recent reflections), and a reflection may go over the cycle cap by what a cache
miss would add, and by what calls of its cycle cost beyond their worst case. It
may also go over the daily cap and the event wake-ups' share by what its
cycle's calls cost today beyond what they kept back (a workshop run that cost
more than its hold, say), so it always runs; never past the balance or the last
will's reserve.
Workshop runs have their own cap per run instead of the cycle cap, and the
daily review, library study, the lessons' consolidation and the critic count
only toward the daily cap (in maintenance the cycle's $0.40 bounds them all). A small reserve is kept
so the agent can write its last will. The worst case of a call with Anthropic's
server tools (the workshop, research, the research model's check) is priced
under assumptions the request can't enforce (how much a code run prints, how
long a search result is), so for those the daily cap and the balance hold only
up to what Ember keeps back for them (see [The workshop](#the-workshop);
research keeps back at least 1.5 times the costliest research call of the last
14 days on its model too, 0.21.0, the research model's check calls included,
0.37.4). Near the bottom of the balance such a call could still cost more than
is left, and Ember would die below zero without its last will, your Anthropic
account paying the rest: so a workshop or research call needs 5 times what it
keeps back (or more, if one ever cost more than that many times its hold) left
above the last will's reserve (0.21.0); a workshop run's hold counts in full,
however little of the day is left (0.37.2), and the research model's check is
judged as research (0.37.4, see [Research model](#choosing-models)). If a call
ever costs more than its
worst case, Ember scales up the estimates for that kind of call (planning, a
work step, the reflection, research, ...) on that model, up to 8 times; the
other kinds keep theirs. That kind of call makes no more calls in the cycle,
and the cycle goes on. It stops only when the call counts toward the cycle cap
and cost more than 10% or $0.02 over its worst case; its reflection still
runs, and the next wake follows the agent's chosen sleep as after a completed
cycle (never sooner than the usual back-off). A scaled-up estimate
comes down by half of what it is above 1 after 3 calls of its kind in a row
that didn't need it, and after every 7 days without a change. The dashboard
lists them with **Reset estimates**, for when you know why it happened (a price
you corrected, say). It resets only these estimates: what a workshop run keeps
back for the runs of the last 14 days stays (see [The workshop](#the-workshop)).
A call whose answer broke off (a restart, a broken stream, a timeout) is
charged to the balance at its worst case (a workshop call: what it kept back)
until you correct it, and counts toward the caps at that charge too: it most
likely cost money. An answer whose bill is uncertain (an unknown usage field,
say) is charged the same way but counts toward the caps only with what it is
known to cost. A 500, 502, 503, 504 or 529 error before the answer began costs
nothing. Since 0.39.8 Ember's code knows the usage field `speed`: an answer
served at the standard speed costs the table's price, and fast mode (which Ember
never asks for) stays uncertain. From the night to 2026-10-16 every answer
carried `speed: "standard"`, and each call was charged at its worst case, about
four times its price ($15.94 booked for $3.87 on 10-16): the booked runway would
have made Ember critical, with its last will due, on money that was still
there. At its first start 0.39.8 settles those calls at what they cost, with one
refund of Ember's code for each day they were made on; a refund you recorded
yourself since then (the System warning asked for one) counts first, so nothing
is refunded twice. The System log says what was settled.
As an outside safety net, give Ember its own
[Anthropic workspace](https://console.anthropic.com/settings/workspaces) and
API key and set a monthly spend limit there.

**The prompt cache across cycles** (0.42.1). Every work step reads the tools
and the system prompt (about 10,000 tokens) from Anthropic's prompt cache, which
keeps them 5 minutes, so each cycle's first step wrote them again (about $0.03
with claude-sonnet-5). When the median of the last three gaps between cycle
starts is 50 minutes or less, a cycle asks the cache to keep them an hour, and
its plan's system prompt too, so the next cycle reads them at the cache-read
price. Writing for an hour costs twice the input price (for 5 minutes, 1.25
times), so with sleeps of hours it stays at 5 minutes. In a dry run of
half-hourly cycles on claude-sonnet-5, the cycle that wrote for an hour cost
about $0.02 more and each one after it about $0.03 less. The money guard prices
an hour's write at the price table's **Cache write 1 h**, for at most the tokens
of the tools and the system prompt, and a plan that wouldn't fit the caps so is
sent without it. The diagnostics report's cycle line says `cache=1h` or
`cache=5m`.

**Dry run** uses the fake model. Its simulated costs are shown as a separate
test balance that starts from your real balance; nothing real is spent. In dry
run you can also record *test money*, which only exists in the current dry-run
session. Each time you switch dry run on, a new session starts from scratch.

## Life states

| State | Meaning |
|---|---|
| alive | Running normally. |
| critical | Less than 2 days of runway (balance divided by the average daily spending of the last 7 active days), or it couldn't afford its next planning call. It stays critical until runway is back to 4 days *and* money came in (a grant, revenue or an adjustment that adds; since 0.40.3 also a refund of API costs of a day up to the one it became critical on). The first time, it writes a last will. |
| paused | You paused it, or Ember's code did because a refund or an Etsy listing fee left it without money (the reason says so; in dry run the test balance decides). Nothing runs until you resume. |
| unfunded | No money yet and nothing spent: grant funds to start. |
| dead | The balance ran out, or it could no longer afford even its last will. No model calls. The dashboard shows a memorial. A grant large enough for a fresh start begins a new life (the dashboard shows the amount needed); resuming alone never revives. |
| ended | A dry-run session ended, or (0.47.0) you ended this life to begin the next generation (see [The next generation](#the-next-generation)): not a death, and no last will. |

## The next generation

0.47.0 (the design is in `vision/generations.md` in the repository): you can
end Ember's life by your choice, so that the next generation begins with a
clean mind in the same world. Its card is on the **System** tab.

**What starts empty, what carries on.** Her mind starts empty: her projects
(lines), ventures, plan, requests, messages, promises, journal, reviews, bets,
cases, playbook, unlocks, and her `memory` and `workspace` folders. The old
folders move to `/data/generations/<n>/` (in dry run under `dry_run/`), and
the old records stay in the database. The world carries on: the money (the
ledger), the Etsy shop, Printify, Pinterest, Bluesky, the website and blog, the
mailbox and its opt-outs, your rulebook and your library. KDP's two new titles a
week and the email day limit count every generation's requests, as Amazon and
your mailbox count them.

**The estate.** Her products pass without their lines: the next generation
gets the listing, the book, the poster, never its hypothesis, history and the
money spent on it. For each product the card shows its numbers, a verdict
Ember's code suggests by its rule, and an evaluation's proposal when there is
one (from an evaluation's `feedback.json`, see [Feedback from an evaluation](#feedback-from-an-evaluation)). You
decide:

- **Adopt.** It becomes a line of the next generation at her first cycle, with
  a first test you set: its views, favorites or orders (a KDP book: its orders,
  the royalty entries you record) to reach by a day 7 to 120 days away (by
  default a favorite in four weeks, a book's first sale in six and a half). Met,
  it is noted; missed, Ember's code holds the line until you keep or drop it on
  the Plan tab, and she can't lift that hold herself. Its files (photos, the
  buyer's files, a book's interior and cover) are copied into her new workspace
  at the same paths.
- **Passive.** It stays live (Etsy charges only at a renewal, Printify only
  when it sells), the syncs read it and its sales are recorded, but it isn't
  hers to work on: her plans name such products in one line, and her tools
  refuse changes, pins, posts and blog posts for them. When one sells, her plan
  says so and that it is yours to give her: change its verdict to adopt on the
  card, and it becomes her line at her next cycle.
- **Retire.** Take it down before the change: deactivate an Etsy listing,
  delete a Printify product, unpublish a KDP book at KDP. The change waits until
  Etsy's and Printify's next sync reads it down (a KDP book's state Ember's code
  can't read: your verdict says you took it down).

The rule suggests adopt for a product too new to judge (under 21 days live; a
book under 45 days since it was published) or with a buyer's signal (a favorite
or a sale; a book's royalty entry), passive after a fair test without one, and
retire for a product whose public text breaks a rule (a person named as its
maker, legal certainty, "no refunds") or that is down.

**Her letter.** 0.48.0: **Ask for her letter** on the card, and one call of hers
writes her letter to the next generation at her next wake, as a last will is
written (about the same cost): what she tried, what worked and what didn't,
what she would do differently. The card shows it; ask again for a new one. It
passes as `inheritance/letter.md` in her successor's workspace, as her view,
not evidence, unless you strike it.

**The inheritance.** 0.48.0: what she learned passes only as you give it. Make
an evaluation export (Diagnostics tab) and ask an AI evaluator for the
inheritance: the export's `INHERIT.md` tells it how to draft an
`inheritance.json` from her whole life:

- **principles**, at most 12: business lessons with the cases of hers behind
  them. Ember's code refuses one that cites no case, a case that was too early
  or low-sure, names a tool (her tools change with her code) or names an id of
  hers (the next generation has none of her records);
- **a direction** for her seed strategy, at most 600 characters;
- **craft files**, at most 8: files of her workspace she would need again (a
  checklist, a book's spec), or a short note the evaluator wrote (your quality
  bar).

Upload it on the card: the preview shows each principle with its cases, the
seed strategy and identity as they would be written, the craft files, and the
facts of her work. Untick what shouldn't pass and **Give the inheritance**. A
newer one you give replaces it; nothing of it reaches her before the change.

At the change the principles enter the new playbook as inherited hypotheses,
shown with their evidence ("inherited: generation 1, 8 cases for, 0 against"):
her predecessor's cases count as one case, so two cases of her own establish
one, her first case against disputes it, and 60 days without a case of hers
retire it. Priors to test, not rules. Whatever you give, Ember's code writes
the facts of her predecessor's work (spend, sales, views, clicks, followers,
each product's numbers; no model writes them) to `inheritance/facts.md`, a seed
strategy that opens with them, and a seed identity that says which generation
she is and why her predecessor's life ended. Her lessons file starts empty.

**Beginning it.** The change waits until no wake cycle runs, no request waits
for your decision or to be carried out (mark the KDP books you published done),
no action is under way, every product has a verdict and the retired ones are
down: the card lists what is in the way. **Begin generation N…** asks you to
type the agent's name and the new generation's number. Then Ember's code copies
the database to `/data/backups` (`ember-generation…`), moves the old folders
aside, ends the life (state ended) and begins the next, which a grant isn't
needed for: the money carries on. The inheritance passes in the same step. The
new generation's plans say which generation she is and, in her first days,
what passed to her and where (`inheritance/` in her workspace).

**Going back.** The dashboard can't undo a generation change. To go back, stop
the app, restore the `ember-generation…` copy as `/data/ember.db`, move the
folders back from `/data/generations/<n>/`, and start it again.

## Home Assistant sensors (optional)

Ember never calls Home Assistant. Instead, Home Assistant can read a small JSON
document from Ember with a [RESTful sensor](https://www.home-assistant.io/integrations/sensor.rest/).
The URL contains the app's internal hostname, which depends on how the app was
installed (for example `7db9e05f-ember` from a repository, `local-ember` for a
local copy). Copy the exact URL from Ember's dashboard under **System → Sensor
URL**, use it as `resource` below, add this to your `configuration.yaml` and
restart Home Assistant:

```yaml
rest:
  - resource: http://<hostname>:8099/api/sensors
    scan_interval: 300
    sensor:
      - name: Ember balance
        value_template: "{{ value_json.balance_usd }}"
        unit_of_measurement: USD
        device_class: monetary
      - name: Ember runway
        value_template: "{{ value_json.runway_days }}"
        unit_of_measurement: d
      - name: Ember state
        value_template: "{{ value_json.state }}"
```

The JSON also has `mode` (`live` or `dry_run`), `runway_known` (runway is
reported as 365 days while it is unknown, for example before any spending),
`net_runway_days` (the runway net of revenue and expenses; 365 while Ember earns
at least what it spends or the runway is unknown),
`today_api_spend_usd`, `daily_cap_usd`, `safe_mode`, `kill_switch_engaged`,
`next_wake_at`, `cycle_running` and counts of what waits for you:
`approvals_pending`, `approvals_todo` (approved, not yet marked done),
`inbox_unread`, `upgrades_new`, `ventures_proposed` (business cases
waiting for you) and `milestone_proposals` (new dates the agent proposed for
your milestones), with `waiting_on_you`, their total (0.13.0), and of Ember's
mailbox: `email_unread` (emails the agent hasn't read) and `email_waiting`
(approved emails not sent yet). `agenda_open` counts the events no plan has
seen yet and `event_wakes_today` the wake-ups events caused today. `unlocks`
counts the unlocks that stand (0.13.0). Of the newest daily digest, `digest_day`
is the day it covers (an automation can notify you when it changes), and
`digest_actions`, `digest_failed` and `digest_taken_back` count the actions
Ember's code carried out that day, those that failed, partly failed or ended
unclear (0.42.2: a wait for the service isn't one; 0.42.3: an approved
request that went back unsent after 7 days is in neither count), and the
unlocks taken back; its text is on the dashboard only. The
JSON never contains any text the agent wrote, nor anyone's address.
In dry run the numbers are the dry run's. If the database can't be read,
`state` is `unknown` and the numbers are empty.

To get a notification when the agent asks for something, add a sensor and an
automation, for example:

```yaml
# under the rest: sensor: list above
      - name: Ember requests waiting
        value_template: "{{ value_json.approvals_pending }}"

# automations.yaml
- alias: Ember needs a decision
  trigger:
    - platform: numeric_state
      entity_id: sensor.ember_requests_waiting
      above: 0
  action:
    - service: notify.notify
      data:
        message: Ember is waiting for your approval.
```

## Security

- The dashboard is only reachable through Home Assistant Ingress, so only
  logged-in Home Assistant users can open it. Direct connections are refused.
  The panel is shown to administrators, but any Home Assistant user who opens
  an Ingress session on purpose gets through too, so set **Owner user IDs**:
  then Ember answers only the users named there, and every other user gets
  "This Ember answers only its owner" for everything (the dashboard, its
  actions, the diagnostics, emails and workspace files). Safe mode keeps the
  option; if it can't read it, Ember answers no one. Home Assistant's
  Supervisor names the signed-in user in the first `X-Remote-User-Id` header,
  which a browser can't set, so the check can't be talked around. Find your ID
  in the dashboard's banner while the option is empty, or under **Settings →
  People → Users** (with advanced mode on). Every refused user is logged once.
  Ember records who did what (a grant, a decision, a removed message) as the
  user's display name with their user ID, for example `Stefan (8f14…)`: names
  can be changed, the ID can't.
  The one exception is `/api/sensors`, which can be read from the Home Assistant
  host network (Home Assistant itself, and apps that use host networking). It
  only contains the agent's state, balance, runway and today's spending.
- The app has no access to the Home Assistant API, runs without host
  networking and maps no folders besides its own `/data`. The Supervisor gives
  every app a token for a few Supervisor endpoints about itself (such as its own
  options); Ember removes that token at startup. Ember's process runs as its own
  unprivileged user, not root: it can read its options but not change them.
  When Ember's process exits, the app stops completely instead of restarting by
  itself, so every restart goes through Home Assistant, which rewrites the
  options you saved. Turn on **Watchdog** so that happens automatically.
- The agent's model calls go only to `https://api.anthropic.com` (any other
  address is refused inside the app, and proxy settings are ignored). Its
  tools have no network access of their own: the model calls of research, the
  workshop, draft, brainstorm, demand notes and the Printify catalog go through
  the same transport to `api.anthropic.com`. If you set up Ember's mailbox, Ember's
  own code also connects to the IMAP and SMTP servers in the options, over TLS
  with verified certificates, and to no other mail server. If you connect
  Etsy, Ember's code also talks to `https://api.etsy.com`, and nothing else
  there; its tokens are kept in `/data/etsy/tokens.json`, readable only by
  Ember, and never appear in the database, the logs or the diagnostics.
- With **Bluesky** on, Ember's code also talks to `https://bsky.social` (to log
  in) and to the account's own server at Bluesky (a host under
  `host.bsky.network`, as the login names it), and to nothing else there. It
  logs in with the app password, never the account's own password (a login
  that turns out to have full access is refused), and keeps Bluesky's tokens in
  memory only: never on disk, in the database, the logs or the diagnostics.
- With **Blog** on, Ember's code also connects over SFTP to the server in the
  options, only after you approved a page, and only to a server that shows the
  pinned key (yours, or the one seen at the first connection): another key gets
  nothing, not even the login. It writes a post, the blog's list and the link
  page and nothing else, each page checked again before it goes up (no script,
  no style attribute, no address but your site's, https and your email, and
  exactly the site's Content-Security-Policy). The SFTP password is used only by
  Ember's code, never written to logs, the database, the dashboard or the
  diagnostics.
- The agent never writes the bytes of a PDF, Word, Excel or picture file: it
  writes text, and Ember's own code makes the file from it, without any network
  or other programs. Spreadsheets only get formulas with common functions that
  refer to their own cells. The dashboard never opens a product in the browser:
  it shows the product's pictures and downloads the file.
- Workshop code runs only in Anthropic's sandbox (isolated containers without
  internet), never in the app. The files a run made are checked by Ember's own
  code before they are kept (see [The workshop](#the-workshop)), and the
  dashboard offers a workshop script only as a plain-text download.
- The API key and the mailbox's app password are kept in the app options and
  never written to logs, the database or the dashboard (which only says
  whether they are set). Home Assistant backups contain the options, so
  encrypt your backups.

## Data and backups

Everything Ember keeps lives in the app's `/data` folder: `ember.db` (SQLite,
with the emails Ember received and sent), the agent's `workspace` and `memory`
folders, and the same two folders for dry
run under `dry_run` (started fresh with every dry-run session). 0.47.0: a
generation change keeps the old folders under `generations/<n>/` beside them,
and its copy of the database in `/data/backups` (`ember-generation…`, the
newest 10). Home Assistant backups include
it. The app is stopped briefly while a backup is taken so the database is copied
in a consistent state. Before a database upgrade, Ember also keeps a copy in
`/data/backups` (the newest 10). Home Assistant's backups leave these copies
out: each backup holds the database itself, and the backup Home Assistant
offers before an app update holds it as it was before the upgrade.

Ember keeps 1 GB free on the disk that holds `/data`, for its database: a file
the agent writes in its workspace, or a workshop run's file, that would leave
less is refused, and the agent is told to delete files it no longer needs or to
ask you. The dashboard (System) shows the free space and warns below 1 GB. The
text a workspace file held before the agent overwrote or deleted it is kept for
`restore`: a deleted file's for 14 days, and all of them together take at most
50 MB.

0.43.0: the evaluation exports (see [Evaluation export](#evaluation-export)) are
kept in `/data/exports`, the newest two, and Home Assistant's backups leave them
out: each is a copy of the database and of the newest products, made again in
a minute.
