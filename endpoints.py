import tornado.web
import tornado.template
import tornado.escape
import tornado.ioloop
from dispatch import *
import os
import json
import starlight
import time
import pytz
import itertools
import enums
import table
from collections import defaultdict
from datetime import datetime, timedelta

import webutil

@route(r"/([0-9]+-[0-9]+-[0-9]+)?")
class Home(HandlerSyncedWithMaster):
    @tornado.web.asynchronous
    def head(self, pretend_date):
        return self.get(pretend_date)

    @tornado.web.asynchronous
    def get(self, pretend_date):
        actually_now = pytz.utc.localize(datetime.utcnow())

        if pretend_date:
            now = pytz.utc.localize(datetime.strptime(pretend_date, "%Y-%m-%d"))
        else:
            now = actually_now

        if now.day == 29 and now.month == 2:
            now += timedelta(days=1)

        self.events = starlight.data.events(now)
        self.event_rewards = self.settings["tle"].lookup_event_rewards(self.events)

        self.gachas = starlight.data.gachas(now)
        self.gacha_limited = starlight.data.limited_availability_cards(self.gachas)

        # Show only cu/co/pa chara birthdays. Chihiro is a minefield and causes
        # problems
        self.birthdays = list(filter(lambda char: 0 < char.type < 4,
                                     starlight.data.potential_birthdays(now)))

        self.recent_history = self.settings["tle"].get_history(10)

        # cache priming has a high overhead so prime all icons at once
        preprime_set = set()
        for h in self.recent_history:
            preprime_set.update(h.card_list())
        starlight.data.cards(preprime_set)

        self.rates = {}
        self.complete = 0
        for gacha in self.gachas:
            if (now >= gacha.start_date) and (now <= gacha.end_date):
                starlight.data.live_gacha_rates(gacha, self.receive_live_gacha_rate)
            else:
                self.complete += 1

        # case where we don't make any calls to live_gacha_rates
        if self.complete == len(self.gachas):
            # we need to return to the ioloop or else a bogus exception gets raised
            tornado.ioloop.IOLoop.current().add_callback(self.complete_for_real)

    def receive_live_gacha_rate(self, rate):
        if rate:
            try:
                self.rates[rate["gacha"]] = rate["rates"]
            finally:
                self.complete += 1
        else:
            self.complete += 1

        if self.complete == len(self.gachas):
            self.complete_for_real()

    def complete_for_real(self):
        self.render("main.html", history=self.recent_history,
            events=zip(self.events, self.event_rewards),
            la_cards=zip(self.gachas, self.gacha_limited),
            live_gacha_rates=self.rates,
            birthdays=self.birthdays, **self.settings)
        self.settings["analytics"].analyze_request(self.request, self.__class__.__name__)

@route("/suggest")
class SuggestNames(HandlerSyncedWithMaster):
    def get(self):
        names = {value.conventional.lower(): [value.conventional, key] for key, value in starlight.data.names.items()}
        names.update({str(key): [value.conventional, key] for key, value in starlight.data.names.items()})

        self.set_header("Content-Type", "application/json")
        self.set_header("Cache-Control", "no-cache")
        self.set_header("Expires", "0")
        self.write(names)

@route(r"/_evt")
class EventD(HandlerSyncedWithMaster):
    def get(self):
        self.set_header("Content-Type", "text/plain; charset=utf-8")

        now = pytz.utc.localize(datetime.utcnow())
        if now.day == 29 and now.month == 2:
            now += timedelta(days=1)

        events = starlight.data.events(now)

        if events:
            evedt = events[0].end_date.astimezone(pytz.timezone("Asia/Tokyo"))
            self.set_header("Content-Type", "text/plain; charset=utf-8")
            self.write("{0}".format(evedt.strftime("%B %d, %Y %H:%M")))
        else:
            self.write("None")

@route(r"/char/([0-9]+)(/table)?")
class Character(HandlerSyncedWithMaster):
    def get(self, chara_id, use_table):
        chara_id = int(chara_id)
        achar = starlight.data.chara(chara_id)

        card_ids = starlight.data.cards_belonging_to_char(chara_id)
        chains = [starlight.data.chain(id) for id in card_ids]
        unique = []
        for c in chains:
            if c not in unique:
                unique.append(c)

        acard = [starlight.data.cards(ch) for ch in unique]
        eventset = {x.id: x for x in starlight.data.event_ids()}

        availability = defaultdict(lambda: [])
        av_dict_from_tle = self.settings["tle"].lookup_event_cards(card_ids)
        for cid, events in av_dict_from_tle.items():
            for eid in events:
                x = eventset[eid]
                avs = starlight.Availability(starlight.Availability._TYPE_EVENT, x.name, x.start_date, x.end_date)
                availability[cid].append(avs)

        ga_av = self.settings["tle"].gacha_availability(card_ids, starlight.data.gacha_ids())
        for k in ga_av:
            availability[k].extend(ga_av[k])

        if achar:
            self.set_header("Content-Type", "text/html")
            self.render("chara.html",
                chara=achar,
                chara_id=chara_id,
                cards=acard,
                use_table=use_table,
                availability=availability,
                now=pytz.utc.localize(datetime.utcnow()),
                **self.settings)
            self.settings["analytics"].analyze_request(
                self.request, self.__class__.__name__, {"chara": achar.conventional})
        else:
            self.set_status(404)
            self.write("Not found.")


@route(r"/card/([0-9\,]+)(/table)?")
class Card(HandlerSyncedWithMaster):
    def get(self, card_idlist, use_table):
        card_ids = [int(x) for x in card_idlist.strip(",").split(",")]

        chains = [starlight.data.chain(id) for id in card_ids]
        unique = []
        for c in chains:
            if c not in unique:
                unique.append(c)

        acard = [starlight.data.cards(ch) for ch in unique if ch]

        eventset = {x.id: x for x in starlight.data.event_ids()}

        availability = defaultdict(lambda: [])
        av_dict_from_tle = self.settings["tle"].lookup_event_cards(card_ids)
        for cid, events in av_dict_from_tle.items():
            for eid in events:
                x = eventset[eid]
                avs = starlight.Availability(starlight.Availability._TYPE_EVENT, x.name, x.start_date, x.end_date)
                availability[cid].append(avs)

        ga_av = self.settings["tle"].gacha_availability(card_ids, starlight.data.gacha_ids())
        for k in ga_av:
            availability[k].extend(ga_av[k])

        if acard:
            if len(acard) == 1:
                just_one_card = acard[0][0]
            else:
                just_one_card = None
            self.set_header("Content-Type", "text/html")
            self.render("card.html", cards=acard, use_table=use_table,
                just_one_card=just_one_card, availability=availability,
                now=pytz.utc.localize(datetime.utcnow()), **self.settings)
            self.settings["analytics"].analyze_request(
                self.request, self.__class__.__name__, {"card_id": card_idlist})
        else:
            self.set_status(404)
            self.write("Not found.")

# all the table handlers go here

# Try to use ShortlinkTable.rendertable instead of directly rendering
# a table template whenever possible, so we can make enhancements to
# it apply globally to all tables.

@route(r"/t/([A-Za-z]+)/([^/]+)")
class ShortlinkTable(HandlerSyncedWithMaster):
    # This shouldn't take too long.
    # The full chain is pre-emptively loaded when any member is requested
    def flip_chain(self, card):
        return starlight.data.card(starlight.data.chain(card.series_id)[-1])

    def rendertable(self, dataset, cards,
                    allow_shortlink=1, table_name="Custom Table",
                    template="generictable.html", **extra):
        if isinstance(dataset, str):
            filters, categories = table.select_categories(dataset)
        else:
            filters, categories = dataset

        should_switch_chain_head = self.get_argument("plus", "NO") == "YES"
        if should_switch_chain_head:
            cards = list(map(self.flip_chain, cards))

        extra.update(self.settings)

        self.render(template,
                    filters=filters,
                    categories=categories,
                    cards=cards,
                    original_dataset=dataset,
                    show_shortlink=allow_shortlink,
                    table_name=table_name,
                    is_displaying_awake_forms=should_switch_chain_head,
                    **extra)

    def get(self, dataset, spec):
        try:
            idlist = webutil.decode_cardlist(spec)
        except ValueError:
            self.set_status(400)
            self.write("The card list could not be parsed")
            return

        self.rendertable(dataset.upper(), starlight.data.cards(idlist))
        self.settings["analytics"].analyze_request(self.request, self.__class__.__name__)

@route(r"/skill_table")
class SkillTable(ShortlinkTable):
    def get(self):
        ds = filter(lambda C: C.skill is not None, starlight.data.cards(starlight.data.all_chain_ids()))
        self.rendertable("CASDE", ds,
            allow_shortlink=0,
            table_name="Cards by skill")
        self.settings["analytics"].analyze_request(self.request, self.__class__.__name__)

@route(r"/lead_skill_table")
class LeadSkillTable(ShortlinkTable):
    def get(self):
        ds = filter(lambda C: C.lead_skill is not None, starlight.data.cards(starlight.data.all_chain_ids()))
        self.rendertable("CAKL", ds,
            allow_shortlink=0,
            table_name="Cards by lead skill")
        self.settings["analytics"].analyze_request(self.request, self.__class__.__name__)

@route(r"/table/([A-Za-z]+)/([0-9\,]+)")
class CompareCard(ShortlinkTable):
    def get(self, dataset, card_idlist):
        card_ids = [int(x) for x in card_idlist.strip(",").split(",")]

        chains = [starlight.data.chain(id) for id in card_ids]
        unique = []
        for c in chains:
            if c[0] not in unique:
                unique.append(c[0])

        acard = starlight.data.cards(unique)

        if acard:
            self.rendertable(dataset.upper(), acard, table_name="Custom Table")
            self.settings["analytics"].analyze_request(
                self.request, self.__class__.__name__, {"card_id": card_idlist})
        else:
            self.set_status(404)
            self.write("Not found.")

@route(r"/gacha(?:/([0-9]+))?")
class GachaTable(ShortlinkTable):
    @tornado.web.asynchronous
    def get(self, maybe_gachaid):
        now = pytz.utc.localize(datetime.utcnow())

        if maybe_gachaid:
            maybe_gachaid = int(maybe_gachaid)
            gachas = starlight.data.gacha_ids()

            for gcid in gachas:
                if gcid.id == maybe_gachaid:
                    selected_gacha = gcid
                    break
            else:
                selected_gacha = None
        else:
            gachas = starlight.data.gachas(now)

            if gachas:
                selected_gacha = gachas[0]
            else:
                selected_gacha = None

        if selected_gacha is None:
            self.set_status(404)
            self.write("Not found. If there's no gacha happening right now, you'll have to specify an ID.")
            return self.finish()

        is_current = (now >= selected_gacha.start_date) and (now <= selected_gacha.end_date)

        self.availability_list = starlight.data.available_cards(selected_gacha)
        self.availability_list.sort(key=lambda x: x.sort_order)

        self.selected_gacha = selected_gacha
        self.card_list = starlight.data.cards(gr.card_id for gr in self.availability_list)
        limited_flags = {gr.card_id: gr.is_limited for gr in self.availability_list}

        self.filters, self.categories = table.select_categories("CASDE")

        lim_cat = table.CustomBool()
        lim_cat.header_text = "Lm?"
        lim_cat.values = limited_flags
        lim_cat.yes_text = "Yes"
        lim_cat.no_text = "No"
        self.categories.insert(0, lim_cat)

        if is_current:
            starlight.data.live_gacha_rates(self.selected_gacha, self.complete_with_rel_odds)
        else:
            self.complete_with_rel_odds(None)

    def complete_with_rel_odds(self, live_info):
        if live_info:
            rel_odds = live_info["indiv"].copy()
            rel_odds.update({self.flip_chain(starlight.data.card(gr.card_id)).id:
                rel_odds.get(gr.card_id, 0.0) for gr in self.availability_list})
            odds_cat = table.CustomNumber(rel_odds, header_text="Chance", format="{0:.3f}%")
            self.categories.insert(1, odds_cat)

            live_rates = live_info["rates"]
        else:
            live_rates = None

        self.rendertable( (self.filters, self.categories),
            cards=self.card_list,
            allow_shortlink=0,
            table_name="Gacha: {0}".format(self.selected_gacha.name),
            template="ext_gacha_table.html",
            gacha=self.selected_gacha,
            rates=live_rates)
        # self.settings["analytics"].analyze_request(self.request, self.__class__.__name__,
        #     {"gid": maybe_gachaid})


@route(r"/sprite_go/([0-9]+).png")
class SpriteRedirect(tornado.web.RequestHandler):
    """ Javascript trampoline to locate transparents' URLs. """

    def get(self, card_id):
        card_id = int(card_id)
        assoc_card = starlight.data.card(card_id)

        assoc_char = assoc_card.chara_id
        assoc_pose = assoc_card.pose
        self.redirect("{0}/chara2/{1}/{2}.png".format(self.settings["image_host"],
                                                     assoc_char, assoc_pose))
        self.settings["analytics"].analyze_request(self.request, self.__class__.__name__,
            {"card_id": "({0}) {1} <{2}>".format(assoc_card.title, assoc_card.chara.conventional, card_id)})


@route(r"/sprite_go_ex/([0-9]+)")
class SpriteViewerEX(tornado.web.RequestHandler):
    def get(self, chara_id):
        achar = starlight.data.chara(int(chara_id))
        if achar:
            svxdata = starlight.data.svx_data(achar.chara_id)
            self.render("spriteviewer.html",
                load="{0}/chara2/{1}".format(self.settings["image_host"], int(chara_id)),
                known_poses=svxdata,
                chara=achar,
                **self.settings)
        else:
            self.set_status(404)
            self.write("Not found.")

@route("/history")
class History(HandlerSyncedWithMaster):
    """ Display all history entries. """
    def get(self):
        all_history = self.settings["tle"].get_history(nent=None)

        preprime_set = set()
        for h in all_history:
            preprime_set.update(h.card_list())
        starlight.data.cards(preprime_set)

        self.render("history.html", history=all_history, **self.settings)
        self.settings["analytics"].analyze_request(self.request, self.__class__.__name__)

@route(r"/tl_cacheall")
@dev_mode_only
class DebugTLCacheUpdate(tornado.web.RequestHandler):
    def get(self):
        self.settings["tle"].update_caches()
        self.write("ok.")

@route(r"/ga_genpresencecache")
@dev_mode_only
class DebugGachaPresenceUpdate(tornado.web.RequestHandler):
    def get(self):
        cl = self.settings["tle"].gen_presence(starlight.data.gacha_ids())
        self.set_header("Content-Type", "text/plain; charset=utf-8")
        self.write("ok")

@route(r"/tl_debug")
@dev_mode_only
class DebugViewTLs(tornado.web.RequestHandler):
    def get(self):
        #chara_id = int(chara_id)
        gen = list((x.key, x.english, x.submitter, time.strftime("%c", time.gmtime(x.submit_utc)))
            for x in filter(lambda x: x.key != x.english, self.settings["tle"].all()))
        fields = ("key", "english", "sender", "ts")

        self.set_header("Content-Type", "text/html")
        self.render("debug_view_database.html", data=gen,
                    fields=fields, **self.settings)

@route(r"/tl_debug/(.+)")
@dev_mode_only
class DebugViewTLExtreme(tornado.web.RequestHandler):
    def get(self, key):
        #chara_id = int(chara_id)
        gen = list((x.key, x.english, x.submitter, time.strftime("%c", time.gmtime(x.submit_utc)))
            for x in self.settings["tle"].all_for_key(key))
        fields = ("key", "english", "sender", "ts")

        self.set_header("Content-Type", "text/html")
        self.render("debug_view_database.html", data=gen,
                    fields=fields, **self.settings)

@route(r"/clear_remote_cache")
@dev_mode_only
class DebugKillCache(tornado.web.RequestHandler):
    def get(self):
        self.settings["tle"].kill_caches(0)
        starlight.data = starlight.DataCache(starlight.data.version)

        self.write("ok.")

@route(r"/sync_event_lookup")
@dev_mode_only
class DebugSyncEventLookup(tornado.web.RequestHandler):
    def get(self):
        self.settings["tle"].sync_event_lookup_table()
        self.write("ok.")

@route(r"/test_gacha_rate")
@dev_mode_only
class DebugAPIGachaRate(tornado.web.RequestHandler):
    def get(self):
        def done(a, b):
            print(b)
            print(a)

        starlight.apiclient.gacha_rates(30180, done)

@route(r"/ping")
class Ping(tornado.web.RequestHandler):
    def head(self):
        return

    def get(self):
        self.write("{} {} {} {} {}".format(
            starlight.data.version,
            starlight.last_version_check,
            len(starlight.data.card_cache),
            len(starlight.data.char_cache),
            "It's working"
        ))
