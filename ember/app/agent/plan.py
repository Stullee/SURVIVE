"""The plan tree (0.34.0): the tree under the owner's goal, laid out and kept by Ember's code.

Live, on 2026-10-07 twelve cycles went to eight things, and a book promised three times got none: READY's ranking,
the obligations, the spending shares and the daily review each decided part of what Ember worked on. Release 2 puts
one tree under the goal. Its projects are platforms (Etsy, KDP, Printify, the website, the channels); each open project
row (a product line) is a product under its platform, laid out from its type's template (templates.py) the first
time this keeper sees it: stages (research, create, release, launch, maintain) and the small steps under them, each
with a check read from Ember's own records, so neither a step nor a stage closes on words. Each cycle, before the
plan, ``keep``:

* adopts new product lines and lays out their stages and steps;
* closes steps and stages whose checks pass (a later stage done closes the ones before it: they are moot);
* puts each promise to the owner that names a product under that product, as a step closed when it is kept;
* adds a live product's recurring steps: a week's pin, a week's Bluesky post (English), a month's blog post (German)
  and the critic's fixes; a missed one stays open and ages;
* closes the products of closed lines.

In 0.34.0 the tree ran in the shadow: each cycle recorded the step it would have taken (weights.py) next to what
READY took. 0.35.0 (Release 2b): the tree steers. ``steer`` takes each cycle's step before the plan, and the step
decides what the cycle is (a marketing step a marketing cycle, any other an ordinary one, unless it is the ventures'
turn); the plan sees it as YOUR STEP (``step_text``). The owner's decisions on a product's requests are steps too
(``_decisions``), taken first like a promise due. 0.35.1: a promise is a step of its own, taken before the heaviest
step and the ventures' turn until it is kept; one made without naming its product gets the one its words name.
0.37.0: a promise and a decision are weighed like any step (``candidates``); 0.37.1: urgent only near their day.
0.37.6: one brake for every step: a step a cycle took without what its check reads moving waits until that moves or
the next day, one whose own requests are on their way waits on them (``candidates``, ``mark``), and a stage that still
needs a request of the owner's gets a step to propose it again (``_again``). 0.37.9: a step whose work one of the
owner's limits would refuse waits on it (``limits``: a pin or a post while the publishing queue is full, a report once
the day's messages are sent; 0.38.1 a listing, a change of one or a product while the sell requests that may wait
for the owner wait), and a promise to report weighs as a report (``_kind``). 0.42.0: each of those rules, and KDP's
week and a first listing's demand note, is the tool's own precondition (preconditions.py), which the plan asks for
the tool a step's work needs: the step waits with the tool's words (``_limited``).

0.42.2 (analysis-0.40.1, 5.6): plan.py was split into modules, one job each: the tree's model, its storage and
its checks (plan_model.py), laying a product out (plan_layout.py), the ventures (plan_ventures.py), what is ready and
its weight (plan_ready.py), the step a cycle takes (plan_steer.py), the keeper (plan_keep.py), what the plan sees of
the tree (plan_texts.py), Ember's and the owner's changes (plan_changes.py) and the owner's Plan tab (plan_view.py).
Every name the rest of Ember and the tests read here is still here; the names tests patch here are read through here
(``_channel_factors``, ``_ventures``, ``_ventured_last``, ``_stale``), and ``busy`` is called through here.
"""

from __future__ import annotations

from .plan_changes import ACTIONS as ACTIONS
from .plan_changes import DECLINE_WORDS as DECLINE_WORDS
from .plan_changes import MAX_NEW_STEPS as MAX_NEW_STEPS
from .plan_changes import STEP_KINDS as STEP_KINDS
from .plan_changes import TITLE_CHARS as TITLE_CHARS
from .plan_changes import WAIT_DAYS as WAIT_DAYS
from .plan_changes import WAIT_ON as WAIT_ON
from .plan_changes import WAITS_A_DAY as WAITS_A_DAY
from .plan_changes import PlanError as PlanError
from .plan_changes import add_steps as add_steps
from .plan_changes import decline_step as decline_step
from .plan_changes import end_product as end_product
from .plan_changes import freeze as freeze
from .plan_changes import frozen as frozen
from .plan_changes import hold as hold
from .plan_changes import line_held as line_held
from .plan_changes import owner_hold as owner_hold
from .plan_changes import owner_keep as owner_keep
from .plan_changes import owner_promise as owner_promise
from .plan_changes import owner_resume as owner_resume
from .plan_changes import owner_rework as owner_rework
from .plan_changes import owner_unwait as owner_unwait
from .plan_changes import owner_words as owner_words
from .plan_changes import pin as pin
from .plan_changes import replace_step as replace_step
from .plan_changes import resume as resume
from .plan_changes import review_held as review_held
from .plan_changes import set_worth as set_worth
from .plan_changes import skip_step as skip_step
from .plan_changes import split_step as split_step
from .plan_changes import step_done as step_done
from .plan_changes import wait_step as wait_step
from .plan_keep import CHANNEL_DUTY as CHANNEL_DUTY
from .plan_keep import REVIEW_CHANGE as REVIEW_CHANGE
from .plan_keep import REVIEW_HOLD as REVIEW_HOLD
from .plan_keep import STOP_ASK_DAYS as STOP_ASK_DAYS
from .plan_keep import _audience_channels as _audience_channels
from .plan_keep import _decide_by as _decide_by
from .plan_keep import keep as keep
from .plan_keep import owner_decision as owner_decision
from .plan_keep import product_milestone as product_milestone
from .plan_layout import infer_audience as infer_audience
from .plan_layout import infer_template as infer_template
from .plan_model import CHANNELS as CHANNELS
from .plan_model import CHECK_EXECUTORS as CHECK_EXECUTORS
from .plan_model import CLICKS_PER_PIN as CLICKS_PER_PIN
from .plan_model import DAY7_VIEWS as DAY7_VIEWS
from .plan_model import FRESH_PINS as FRESH_PINS
from .plan_model import FRESH_POSTS as FRESH_POSTS
from .plan_model import MOVED_KEY as MOVED_KEY
from .plan_model import OBLIGATION_HOURS as OBLIGATION_HOURS
from .plan_model import PROMISE_TRIES as PROMISE_TRIES
from .plan_model import PUSH_DAYS as PUSH_DAYS
from .plan_model import RESULT_DAYS as RESULT_DAYS
from .plan_model import SCALE as SCALE
from .plan_model import Facts as Facts
from .plan_model import _close as _close
from .plan_model import _cut as _cut
from .plan_model import _insert as _insert
from .plan_model import check_words as check_words
from .plan_model import current_stage_name as current_stage_name
from .plan_model import node as node
from .plan_model import nodes as nodes
from .plan_ready import CALLED as CALLED
from .plan_ready import PUBLISHES as PUBLISHES
from .plan_ready import REFUSED as REFUSED
from .plan_ready import Candidate as Candidate
from .plan_ready import _channel_factors as _channel_factors
from .plan_ready import _kind as _kind
from .plan_ready import _limited as _limited
from .plan_ready import _made_in as _made_in
from .plan_ready import _missed as _missed
from .plan_ready import _owed_urgency as _owed_urgency
from .plan_ready import _stale as _stale
from .plan_ready import _views_after as _views_after
from .plan_ready import _words as _words
from .plan_ready import candidates as candidates
from .plan_ready import limits as limits
from .plan_ready import mark as mark
from .plan_steer import Steer as Steer
from .plan_steer import _works as _works
from .plan_steer import busy as busy
from .plan_steer import choose as choose
from .plan_steer import on_channel as on_channel
from .plan_steer import record as record
from .plan_steer import settings as settings
from .plan_steer import steer as steer
from .plan_texts import NEW_PRODUCT as NEW_PRODUCT
from .plan_texts import TRIED as TRIED
from .plan_texts import WAITS as WAITS
from .plan_texts import focus_text as focus_text
from .plan_texts import plan_text as plan_text
from .plan_texts import step_text as step_text
from .plan_ventures import BRAINSTORM_TEMPLATE as BRAINSTORM_TEMPLATE
from .plan_ventures import VENTURE_STEPS as VENTURE_STEPS
from .plan_ventures import VENTURE_TEMPLATES as VENTURE_TEMPLATES
from .plan_ventures import _ventured_last as _ventured_last
from .plan_ventures import _ventures as _ventures
from .plan_ventures import new_things_held as new_things_held
from .plan_ventures import venture_of as venture_of
from .plan_ventures import ventures_node as ventures_node
from .plan_view import channels_from as channels_from
from .plan_view import stamp as stamp
from .plan_view import view as view
