"""App options.

The Supervisor writes the options the owner set in the Home Assistant UI to
``/data/options.json`` (already type-checked against the ``schema`` in
``config.yaml``). This module re-validates them, applies cross-field rules, and
makes sure the API key can never leak through ``repr``, logs or the API.

If the options are invalid the app does not crash: it starts in *safe mode*
(built-in defaults, dry-run forced on) and shows the errors in the dashboard.
0.15.0: safe mode keeps the owner's identity and the kill switch's reset, and
the rules between options that the schema can't check are corrected instead.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator

from . import paths

log = logging.getLogger(__name__)


# The smallest price the options accept: a price of 0 would make calls look free
# and switch every spending limit off.
MIN_PRICE = 0.000001
# 0.13.0: the website's address (https, a host and at most a path: no query, no fragment) and its email address.
# 0.15.0: with their lengths, and empty allowed: config.yaml's schema has the same patterns (a test checks), so Home
# Assistant refuses a bad value when the owner saves it, instead of Ember starting in safe mode. The path names a
# folder, not a file: its last part has no dot (a home page's index.html or index.htm, in any case, is taken off).
_SITE_URL = re.compile(
    r"^(?=.{0,200}$)(?:https://[A-Za-z0-9.-]{1,190}(?::\d{1,5})?"
    r"(?:/(?:[A-Za-z0-9._~-]*/)*(?:[A-Za-z0-9_~-]*|(?i:index\.html?)))?)?$"
)
_SITE_EMAIL = re.compile(r"""^(?=.{0,254}$)(?:[^@\s<>"']{1,64}@[^@\s<>"']{1,190}\.[A-Za-z]{2,63})?$""")
# 0.14.0's SFTP host name, bounded the same way (empty allowed, its length in the pattern).
_HOST = re.compile(r"^(?=.{0,200}$)(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,198}[A-Za-z0-9])?)?$")


class ModelPrice(BaseModel):
    """USD per million tokens for one model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(min_length=1, max_length=100)
    input: float = Field(ge=MIN_PRICE, le=1000)
    output: float = Field(ge=MIN_PRICE, le=1000)
    cache_write_5m: float = Field(ge=MIN_PRICE, le=1000)
    cache_write_1h: float = Field(ge=MIN_PRICE, le=1000)
    cache_read: float = Field(ge=MIN_PRICE, le=1000)

    @field_validator("model")
    @classmethod
    def _strip_model(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _plausible(self) -> ModelPrice:
        # Anthropic's price structure; a row that breaks it is almost certainly a typo
        # (for example a price per thousand tokens instead of per million).
        if not self.cache_read <= self.input <= self.cache_write_5m <= self.cache_write_1h:
            raise ValueError(
                f"prices for {self.model} must satisfy cache_read <= input <= cache_write_5m <= cache_write_1h"
            )
        if self.output < self.input:
            raise ValueError(f"prices for {self.model}: output must not be cheaper than input")
        return self


# Defaults from https://platform.claude.com/docs/en/about-claude/pricing
# (checked 2026-09-27). Prices change: the owner should verify them and can
# edit them in the app options. Must match the defaults in config.yaml.
DEFAULT_PRICE_TABLE: tuple[ModelPrice, ...] = (
    ModelPrice(model="claude-sonnet-5", input=2.0, output=10.0, cache_write_5m=2.5, cache_write_1h=4.0, cache_read=0.2),
    ModelPrice(model="claude-opus-5-5", input=4.0, output=20.0, cache_write_5m=5.0, cache_write_1h=8.0, cache_read=0.2),
    # 0.12.0: priced out of the box as a research model (research_model), once its check passes
    ModelPrice(
        model="claude-haiku-4-5", input=1.0, output=5.0, cache_write_5m=1.25, cache_write_1h=2.0, cache_read=0.1
    ),
)

# Published prices of models the owner is likely to configure (same source and date).
# Only used to warn about configured prices that look far too low.
REFERENCE_PRICES: dict[str, ModelPrice] = {
    p.model: p
    for p in (
        *DEFAULT_PRICE_TABLE,
        ModelPrice(
            model="claude-haiku-4-5-20251001",
            input=1.0,
            output=5.0,
            cache_write_5m=1.25,
            cache_write_1h=2.0,
            cache_read=0.1,
        ),
        ModelPrice(
            model="claude-opus-5", input=5.0, output=25.0, cache_write_5m=6.25, cache_write_1h=10.0, cache_read=0.5
        ),
        ModelPrice(
            model="claude-opus-5-5", input=4.0, output=20.0, cache_write_5m=5.0, cache_write_1h=8.0, cache_read=0.2
        ),
        ModelPrice(
            model="claude-sonnet-4-6", input=3.0, output=15.0, cache_write_5m=3.75, cache_write_1h=6.0, cache_read=0.3
        ),
    )
}
REFERENCE_WEB_SEARCH_USD_PER_1000 = 10.0


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    anthropic_api_key: SecretStr = SecretStr("")
    agent_name: str = Field(default="Ember", min_length=1, max_length=40)
    starting_balance_usd: float = Field(default=20.0, ge=0, le=100_000)
    # 0.12.0: the defaults leave room (a working cycle can cost up to about $0.25; the cycle cap was exactly that, so
    # any addition to the prompts or a higher safety factor stopped scheduled wake-ups).
    daily_spend_cap_usd: float = Field(default=1.5, ge=0, le=1_000)
    cycle_spend_cap_usd: float = Field(default=0.5, ge=0, le=1_000)
    # Ventures (0.10.0): this percent of each day's spending goes to venture cycles, where the agent researches new
    # ways to earn and brings the owner business cases. 0: no venture cycles.
    venture_share: int = Field(default=25, ge=0, le=100)
    # 0.28.0: this percent of each day's spending goes to marketing cycles, which bring buyers to one product line's
    # listings each (pins, posts, blog posts, better titles and tags). 0: no marketing cycles; ordinary cycles market.
    marketing_share: int = Field(default=20, ge=0, le=100)
    # 0.18.0: how the burn modes treat a shrinking runway (economy/burn.py): invest keeps explore until the last will,
    # steady goes no lower than focus, conserve is the burn modes of 0.12.0 to 0.17.0.
    spending_stance: Literal["invest", "steady", "conserve"] = "invest"
    # 0.13.0: the cash the owner would put into a venture's first test; a business case that needs more to start is
    # knocked out (Ember's code won't propose it) until the owner overrides that on the Ventures tab.
    venture_cash_eur: float = Field(default=20.0, ge=0, le=100_000)
    # The library (0.12.0): what Ember may spend a day studying the documents its owner adds (it counts toward the
    # daily cap, not the cycle cap). 0: nothing is studied; the documents can still be searched and read.
    library_study_usd_per_day: float = Field(default=0.5, ge=0, le=100)
    wake_interval_minutes: int = Field(default=240, ge=5, le=10_080)
    min_sleep_minutes: int = Field(default=30, ge=5, le=10_080)
    max_sleep_minutes: int = Field(default=1_440, ge=5, le=10_080)
    max_tool_steps: int = Field(default=15, ge=1, le=100)
    planner_model: str = Field(default="claude-sonnet-5", min_length=1, max_length=100)
    worker_model: str = Field(default="claude-sonnet-5", min_length=1, max_length=100)
    # 0.12.0: model routing. The venture cycles' plans and the daily review on their own model (empty: the planner
    # model), and research on its own (empty: the worker model), once a paired check shows it finds as much.
    strategy_model: str = Field(default="", max_length=100)
    research_model: str = Field(default="", max_length=100)
    price_table: tuple[ModelPrice, ...] = DEFAULT_PRICE_TABLE
    web_search_usd_per_1000: float = Field(default=10.0, ge=MIN_PRICE, le=1_000)
    dry_run: bool = True
    web_fetch: bool = False
    # A message from the owner wakes the agent to read it (0.15.0: one cycle a few minutes after their last message or
    # decision).
    wake_on_message: bool = True
    # 0.12.0: the owner's decision on a request, venture or milestone wakes the agent to act on it, like a message.
    wake_on_decision: bool = True
    # 0.15.0: an urgent event in the agenda (a reply, an inquiry, a milestone's last day) wakes the agent for a short
    # reactive cycle. Off: it waits for the next cycle's plan.
    wake_on_events: bool = True
    # Effort for the work steps and the reflection ("default" sends none, which means high). Not sent to Haiku 4.5.
    worker_effort: Literal["default", "high", "medium", "low"] = "default"
    # The workshop (0.7.0): code the agent has written and run in Anthropic's sandbox. A run has its own cap and
    # counts toward the daily cap, not the cycle cap (the daily cap still bounds it, whatever this cap says). 0.15.0:
    # each call of a run holds at least this cap of the daily cap and the balance, and a run priced above it is
    # refused; the default rose from 0.50 as a run's output is priced per sampling (about $0.60 with Sonnet 5).
    # Container time costs this much per hour after Anthropic's free hours (1,550 a month per organization); Ember
    # books it for every run, as an upper bound.
    workshop: bool = True
    # The model that writes the workshop's code; empty: the worker model.
    workshop_model: str = Field(default="", max_length=100)
    workshop_run_cap_usd: float = Field(default=0.75, ge=0.05, le=100)
    workshop_runs_per_day: int = Field(default=6, ge=0, le=50)
    code_execution_usd_per_hour: float = Field(default=0.05, ge=0, le=100)
    kill_switch_reset: int = Field(default=0, ge=0, le=1_000_000)
    # The Home Assistant users who are Ember's owner (0.11.2): only their requests reach the dashboard and its
    # actions (app/security.py). Empty: every user who can open the panel counts as the owner, and the dashboard says
    # so with the user's own ID to put here.
    owner_user_ids: tuple[str, ...] = ()
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    # Ember's own mailbox (0.4.0). Only types and ranges are checked here: a mailbox that is switched on but
    # incomplete or wrong is reported by the integration (app/integrations/mail.py), never by safe mode.
    email_enabled: bool = False
    email_address: str = ""
    email_password: SecretStr = SecretStr("")
    email_imap_host: str = "imap.mailbox.org"
    email_imap_port: int = Field(default=993, ge=1, le=65_535)
    email_smtp_host: str = "smtp.mailbox.org"
    email_smtp_port: int = Field(default=465, ge=1, le=65_535)
    email_owner_name: str = ""
    email_daily_limit: int = Field(default=3, ge=0, le=20)
    # 0.22.1: the authserv-ids of the owner's mail provider (comma-separated; "none": its header has none, as
    # Outlook's): only an Authentication-Results header of one of them is its verdict (mail._authenticated).
    email_authserv_id: str = Field(default="", max_length=300)
    # Etsy (0.8.0): Ember's code lists approved products in the owner's Etsy shop, through the owner's own Etsy app
    # (its keystring and shared secret) and a one-time connection made in the dashboard. Like the mailbox, a
    # connection that is switched on but incomplete is reported by the integration, never by safe mode.
    etsy_enabled: bool = False
    etsy_keystring: str = Field(default="", max_length=100)
    etsy_shared_secret: SecretStr = SecretStr("")
    etsy_redirect_uri: str = Field(default="https://localhost/ember-etsy", min_length=1, max_length=300)
    etsy_listings_per_day: int = Field(default=3, ge=0, le=20)
    # 0.12.0: keep a daily history of the listings' views and favorites (observations). Off until the owner has
    # confirmed that Etsy's API terms allow keeping it; the shop's own counts are kept either way.
    etsy_stats_history: bool = False
    # 0.12.0: turn Etsy's automatic renewal on for a listing once it has sold (USD 0.20 every four months): listings
    # are created without it, so every one, the ones that sell too, expired after four months.
    etsy_auto_renew_sold: bool = True
    # 0.12.0: record the revenue of paid orders with Ember's listings, Etsy's fees on them and their refunds in the
    # ledger at each sync (created_by 'etsy'), instead of waiting for the owner to. Off until the owner turns it on
    # (audited); EUR amounts need the owner's exchange rate (USD per 1 EUR), 0 leaves them to the owner.
    etsy_auto_record_revenue: bool = False
    etsy_usd_per_eur: float = Field(default=0.0, ge=0, le=3)
    # 0.12.0: a demand note may read Etsy's search of active listings for its keywords (how many match, their price
    # quartiles: aggregates only). Off until the owner has confirmed that Etsy's API terms allow this use; without it,
    # the owner's keyword export in the library (or research) is the source.
    etsy_market_probe: bool = False
    # Pinterest (0.13.0, Phase E2): pins that bring buyers to the Etsy shop, from the owner's account, through their
    # own Pinterest app (its id and secret) and a one-time connection made in the dashboard. Off until the owner turns
    # it on (in dry run too: then a fake account stands in).
    pinterest_enabled: bool = False
    pinterest_app_id: str = Field(default="", max_length=100)
    pinterest_app_secret: SecretStr = SecretStr("")
    pinterest_redirect_uri: str = Field(default="https://localhost/ember-pinterest", min_length=1, max_length=300)
    pinterest_pins_per_day: int = Field(default=3, ge=0, le=20)
    # 0.30.1: Pinterest's API sandbox, for the owner's Standard access request: an app with trial access may not make
    # pins at api.pinterest.com, and Pinterest's review asks for a video of the app connecting and pinning. While it is
    # on (live), the connection goes to api-sandbox.pinterest.com (tokens of its own), the agent has no Pinterest, and
    # a test pin waits for the owner's approval once they connect. Off by default.
    pinterest_sandbox: bool = False
    # Bluesky (0.19.0): posts that bring people to Ember's work, from the account the owner made for it, through an app
    # password made in the account's settings (never its own password). Off until the owner turns it on (in dry run
    # too: then a fake account stands in).
    bluesky_enabled: bool = False
    bluesky_handle: str = Field(default="", max_length=253)
    bluesky_app_password: SecretStr = SecretStr("")
    bluesky_posts_per_day: int = Field(default=2, ge=0, le=20)
    # Printify (0.13.0, Phase E4): physical products with Ember's designs, made on order and sold in the owner's Etsy
    # shop through Printify's Etsy connection, with the owner's personal access token. Off until the owner turns it on
    # (in dry run too: then a fake account stands in). printify_shop_id 0: the one Printify shop connected to Etsy;
    # printify_currency: the currency of the prices and costs Printify shows for it. 0.15.0: who pays the shipping
    # (as the shop's Etsy shipping profile charges it) and whether Printify's bill carries VAT, for the margin check.
    printify_enabled: bool = False
    printify_api_token: SecretStr = SecretStr("")
    printify_shop_id: int = Field(default=0, ge=0, le=999_999_999_999)
    printify_currency: Literal["EUR", "USD", "GBP"] = "EUR"
    printify_products_per_day: int = Field(default=2, ge=0, le=10)
    printify_buyer_pays_shipping: bool = False
    printify_bill_vat: bool = True
    # The owner's website (0.13.0, Phase E3): pages the agent writes, built by Ember's code into a static site that the
    # owner previews, downloads and publishes (Ember never does). Its Impressum and privacy page are made from these:
    # the owner's name, address (lines separated by commas), email and, if they have them, phone and VAT ID; their web
    # host for the privacy page; the site's name (default: the owner's), its language and its address (https).
    site_enabled: bool = False
    site_name: str = Field(default="", max_length=60)
    site_language: Literal["de", "en"] = "de"
    site_url: str = Field(default="", max_length=200)
    site_owner_name: str = Field(default="", max_length=100)
    site_address: str = Field(default="", max_length=300)
    site_email: str = Field(default="", max_length=254)
    site_phone: str = Field(default="", max_length=40)
    site_vat_id: str = Field(default="", max_length=20)
    site_host: str = Field(default="", max_length=200)
    # The blog on the owner's own website (0.14.0): posts and a link page the agent writes, rendered by Ember's code in
    # the site's design (with the site_* data above: its name, address, the owner's name and town, the email) and
    # uploaded by Ember's code over SFTP once the owner approved them. The login is the owner's; blog_sftp_host_key
    # pins the server's key (empty: the first one seen is kept); blog_sftp_folder is the website's folder on the
    # server (empty: the one the login opens). Off until the owner turns it on (in dry run too: a fake server stands
    # in).
    blog_enabled: bool = False
    blog_sftp_host: str = Field(default="", max_length=200)
    blog_sftp_port: int = Field(default=22, ge=1, le=65535)
    blog_sftp_user: str = Field(default="", max_length=100)
    blog_sftp_password: SecretStr = SecretStr("")
    blog_sftp_host_key: str = Field(default="", max_length=800)
    blog_sftp_folder: str = Field(default="", max_length=200)
    # Amazon KDP (0.25.0): books the agent makes (an ebook's Word manuscript and cover, a paperback's interior and full
    # cover), checked by Ember's code against KDP's rules and, once the owner approved one, published by the owner at
    # kdp.amazon.com from their own account (Amazon has no API for KDP). kdp_author: the author name the books carry
    # (empty: the owner enters it at KDP). Off until the owner turns it on.
    kdp_enabled: bool = False
    kdp_author: str = Field(default="", max_length=100)
    # 0.16.0: Ember live on the owner's website: a page (live.html) and a banner for the home page (live/banner.svg)
    # that Ember's code renders from its own numbers and uploads over the blog's SFTP login every 15 minutes, without
    # asking each time. Off until the owner turns it on; each part below can then be switched off. The two parts that
    # can show the agent's own words are off until the owner turns them on, and even then show only what the owner
    # approved: the titles of its ventures and milestones (work) and its last will (memorial).
    live_enabled: bool = False
    live_banner: bool = True
    live_show_money: bool = True
    live_show_revenue: bool = True
    live_show_grants: bool = True
    live_show_chart: bool = True
    live_show_work: bool = False
    live_show_shop: bool = True
    live_show_record: bool = True
    live_show_memorial: bool = False

    @field_validator("owner_user_ids", mode="before")
    @classmethod
    def _user_ids(cls, value: Any) -> Any:
        if not isinstance(value, list | tuple):
            return value
        ids = tuple(v.strip() for v in value if isinstance(v, str) and v.strip())
        if any(len(v) > 100 for v in ids):
            raise ValueError("a Home Assistant user ID has at most 100 characters")
        return ids

    @field_validator(
        "anthropic_api_key",
        "email_password",
        "etsy_shared_secret",
        "pinterest_app_secret",
        "bluesky_app_password",
        "printify_api_token",
        "blog_sftp_password",
        mode="before",
    )
    @classmethod
    def _strip_key(cls, value: Any) -> Any:
        # A key pasted with a stray space or newline would otherwise look "set"
        # but fail every API call.
        return value.strip() if isinstance(value, str) else value

    @field_validator(
        "agent_name",
        "planner_model",
        "worker_model",
        "strategy_model",
        "research_model",
        "workshop_model",
        "email_address",
        "email_imap_host",
        "email_smtp_host",
        "email_owner_name",
        "email_authserv_id",
        "etsy_keystring",
        "etsy_redirect_uri",
        "pinterest_app_id",
        "pinterest_redirect_uri",
        "bluesky_handle",
        "site_name",
        "site_url",
        "site_owner_name",
        "site_address",
        "site_email",
        "site_phone",
        "site_vat_id",
        "site_host",
        "blog_sftp_host",
        "blog_sftp_user",
        "blog_sftp_host_key",
        "blog_sftp_folder",
        "kdp_author",
        mode="before",
    )
    @classmethod
    def _strip(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("log_level", mode="before")
    @classmethod
    def _lower(cls, value: Any) -> Any:
        return value.lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _cross_field_rules(self) -> Settings:
        problems: list[str] = []
        if self.cycle_spend_cap_usd > self.daily_spend_cap_usd:
            problems.append("cycle_spend_cap_usd must not be larger than daily_spend_cap_usd")
        if self.min_sleep_minutes > self.max_sleep_minutes:
            problems.append("min_sleep_minutes must not be larger than max_sleep_minutes")
        elif not self.min_sleep_minutes <= self.wake_interval_minutes <= self.max_sleep_minutes:
            problems.append("wake_interval_minutes must be between min_sleep_minutes and max_sleep_minutes")
        if 0 < self.etsy_usd_per_eur < 0.5:  # 0.12.0: the ledger's range for an exchange rate (0: none)
            problems.append("etsy_usd_per_eur must be 0 (no rate) or between 0.5 and 3 USD per EUR")
        if self.site_url and not _SITE_URL.match(self.site_url):  # 0.13.0: the website's own address
            problems.append(
                "site_url must be an https address without a query or a file name, like https://example.org"
            )
        if self.site_email and not _SITE_EMAIL.match(self.site_email):
            problems.append("site_email must be an email address, like shop@example.org")
        if self.blog_sftp_host and not _HOST.match(self.blog_sftp_host):  # 0.14.0
            problems.append("blog_sftp_host must be a host name like ssh.example.org, without a user or a path")
        names = [p.model for p in self.price_table]
        if len(names) != len(set(names)):
            problems.append("price_table lists the same model more than once")
        for role in ("planner_model", "worker_model", "strategy_model", "research_model", "workshop_model"):
            model = getattr(self, role)
            optional = role in ("strategy_model", "research_model", "workshop_model")
            if (model or not optional) and self.price_for(model) is None:
                problems.append(f"{role} '{model}' has no entry in price_table")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def price_for(self, model: str) -> ModelPrice | None:
        """Exact match only: an unpriced model must never be treated as free."""
        for price in self.price_table:
            if price.model == model:
                return price
        return None

    def price_warnings(self) -> list[str]:
        """Configured prices far below Anthropic's published ones (likely typos)."""
        warnings = []
        for price in self.price_table:
            reference = REFERENCE_PRICES.get(price.model)
            if reference is None:
                continue
            for name in ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read"):
                if getattr(price, name) < getattr(reference, name) / 2:
                    warnings.append(
                        f"{price.model} {name} is {getattr(price, name)} USD per million tokens; "
                        f"Anthropic's published price is {getattr(reference, name)}"
                    )
        if self.web_search_usd_per_1000 < REFERENCE_WEB_SEARCH_USD_PER_1000 / 2:
            warnings.append(
                f"web search is {self.web_search_usd_per_1000} USD per 1,000 searches; "
                f"the published price is {REFERENCE_WEB_SEARCH_USD_PER_1000}"
            )
        return warnings

    @property
    def api_key_set(self) -> bool:
        return bool(self.anthropic_api_key.get_secret_value().strip())

    @property
    def email_password_set(self) -> bool:
        return bool(self.email_password.get_secret_value().strip())

    def public_dict(self) -> dict[str, Any]:
        """Options safe to show in the dashboard. The API key, the passwords and Etsy's keystring (half of Ember's
        Etsy API key, 0.11.2) are replaced by flags."""
        secret = {
            "anthropic_api_key",
            "email_password",
            "etsy_shared_secret",
            "etsy_keystring",
            "pinterest_app_secret",
            "bluesky_app_password",
            "printify_api_token",
            "blog_sftp_password",
        }
        data = self.model_dump(mode="json", exclude=secret)
        data["anthropic_api_key_set"] = self.api_key_set
        data["email_password_set"] = self.email_password_set
        data["etsy_shared_secret_set"] = bool(self.etsy_shared_secret.get_secret_value().strip())
        data["etsy_keystring_set"] = bool(self.etsy_keystring.strip())
        data["pinterest_app_secret_set"] = bool(self.pinterest_app_secret.get_secret_value().strip())
        data["bluesky_app_password_set"] = bool(self.bluesky_app_password.get_secret_value().strip())
        data["printify_api_token_set"] = bool(self.printify_api_token.get_secret_value().strip())
        data["blog_sftp_password_set"] = bool(self.blog_sftp_password.get_secret_value().strip())
        return data


@dataclass(frozen=True)
class LoadedSettings:
    settings: Settings
    errors: list[str] = field(default_factory=list)
    source: str = "defaults"
    # 0.15.0: rules between options that Ember corrected instead of starting safe mode (see _corrected), and, in safe
    # mode, whether owner_user_ids couldn't be read (then Ember answers no one, app/security.py) and whether
    # kill_switch_reset couldn't be (then its value is never stored, app/main.py).
    corrections: list[str] = field(default_factory=list)
    owner_unknown: bool = False
    reset_unknown: bool = False

    @property
    def safe_mode(self) -> bool:
        return bool(self.errors)


def _format_validation_error(exc: ValidationError) -> list[str]:
    messages = []
    for err in exc.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in err["loc"]) or "options"
        messages.append(f"{location}: {err['msg']}")
    return messages


def load_settings(path: Path | None = None) -> LoadedSettings:
    """Read and validate the options file. Never raises."""
    path = path or paths.options_path()
    if not path.exists():
        return LoadedSettings(Settings(), source="defaults (no options file)")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("options file must contain a JSON object")
    except (OSError, ValueError) as exc:
        return _safe_mode([f"could not read {path.name}: {exc}"], None)
    values, corrections = _corrected(raw)
    try:
        settings = Settings.model_validate(values)
    except ValidationError as exc:
        return _safe_mode(_format_validation_error(exc), raw)
    for correction in corrections:
        log.error("Options corrected: %s", correction)
    return LoadedSettings(settings, source=str(path), corrections=corrections)


def _corrected(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """0.15.0: the rules between options that Home Assistant's schema can't check, corrected instead of starting safe
    mode: the cycle cap at most the daily cap, the longest sleep at least the shortest (the shortest wins, as when
    Ember sleeps), the default sleep between them, and an exchange rate below 0.5 as none. Each correction spends no
    more than the owner's options would; the dashboard shows it until the options are fixed. A required text of spaces
    only, which the schema's str(1,x) lets through, becomes its default. A value outside its own range is left for the
    model to refuse (safe mode): Home Assistant refuses it before, so only a hand-edited file has one."""
    values = dict(raw)
    notes: list[str] = []

    def number(key: str) -> float | None:
        info = Settings.model_fields[key]
        value = values.get(key, info.default)
        if not isinstance(value, int | float) or isinstance(value, bool):
            return None
        limits = {type(m).__name__: m for m in info.metadata}
        return value if limits["Ge"].ge <= value <= limits["Le"].le else None  # NaN is in no range

    for key, info in Settings.model_fields.items():
        value = values.get(key)
        if isinstance(value, str) and not value.strip() and any(getattr(m, "min_length", 0) for m in info.metadata):
            values[key] = info.default
            notes.append(f"{key} is blank: the default {info.default} is used")

    daily, cycle = number("daily_spend_cap_usd"), number("cycle_spend_cap_usd")
    if daily is not None and cycle is not None and cycle > daily:
        values["cycle_spend_cap_usd"] = daily
        notes.append(f"cycle_spend_cap_usd ({cycle:g}) is larger than daily_spend_cap_usd: the cycle cap is {daily:g}")
    shortest, longest = number("min_sleep_minutes"), number("max_sleep_minutes")
    default = number("wake_interval_minutes")
    if shortest is not None and longest is not None and default is not None:
        if shortest > longest:
            values["max_sleep_minutes"] = longest = shortest
            notes.append(f"max_sleep_minutes is smaller than min_sleep_minutes: the longest sleep is {shortest:g}")
        if not shortest <= default <= longest:
            values["wake_interval_minutes"] = fitted = min(max(default, shortest), longest)
            notes.append(
                f"wake_interval_minutes ({default:g}) must be between min_sleep_minutes and max_sleep_minutes:"
                f" the default sleep is {fitted:g}"
            )
    rate = number("etsy_usd_per_eur")
    if rate is not None and 0 < rate < 0.5:
        values["etsy_usd_per_eur"] = 0
        notes.append(f"etsy_usd_per_eur ({rate:g}) must be 0 or 0.5 to 3: no rate is used, orders in EUR stay yours")
    return values, notes


def safe_mode_keeps(owner_unknown: bool) -> str:
    """0.15.0: what safe mode keeps of the owner's options, for its log line and its event."""
    if owner_unknown:
        return "owner_user_ids couldn't be read, so Ember answers no one; the kill switch is kept"
    return "owner_user_ids and the kill switch are kept"


def _safe_mode(errors: list[str], raw: dict[str, Any] | None) -> LoadedSettings:
    """Built-in defaults with dry run forced on. 0.15.0: but the owner's identity and the kill switch's reset, each read
    on its own, so one bad option neither opens the dashboard to every user nor lifts a kill switch (app/main.py
    never lifts one in safe mode). If owner_user_ids can't be read, Ember answers no one (owner_unknown)."""
    for error in errors:
        log.error("Invalid option: %s", error)
    kept: dict[str, Any] = {}
    for key in ("owner_user_ids", "kill_switch_reset"):
        if raw is not None and key in raw:
            with contextlib.suppress(ValidationError):
                kept[key] = getattr(Settings.model_validate({key: raw[key]}), key)
    owner_unknown = raw is None or ("owner_user_ids" in raw and "owner_user_ids" not in kept)
    reset_unknown = raw is None or ("kill_switch_reset" in raw and "kill_switch_reset" not in kept)
    log.error("Starting in safe mode: built-in defaults, dry-run forced on; %s.", safe_mode_keeps(owner_unknown))
    return LoadedSettings(
        Settings(dry_run=True, **kept),
        errors=errors,
        source="safe mode (built-in defaults)",
        owner_unknown=owner_unknown,
        reset_unknown=reset_unknown,
    )
