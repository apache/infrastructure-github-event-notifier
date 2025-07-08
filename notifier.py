#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

""" Notifies projects via email about GitHub activities """
import glob
import asfpy.pubsub
import asfpy.messaging
import asfpy.syslog
import yaml
import yaml.parser
import os
import uuid
import git
import re
import time
import typing
import requests
import asyncio


print = asfpy.syslog.Printer(identity="github-event-notifier")

CONFIG_FILE = "github-event-notifier.yaml"
KNOWN_BOTS_FILE = "known-robots.txt"
SEND_EMAIL = True
RE_PROJECT = re.compile(r"(?:incubator-)?([^-]+)")
RE_JIRA_TICKET = re.compile(r"\b([A-Z0-9]+-\d+)\b")
DEFAULT_DIFF_WAIT = 10
DEBUG = False
DIFF_COMMENT_BLURB = """
##########
%(filename)s:
##########
%(diff)s

Review Comment:
%(text)s
"""
JIRA_CREDENTIALS = "/x1/jiratoken.txt"
JIRA_TOKEN = open(JIRA_CREDENTIALS).read().strip()
JIRA_HEADERS = {
    "Content-type": "application/json",
    "Accept": "*/*",
    "Authorization": f"Bearer {JIRA_TOKEN}",
}

def is_bot(userid: str):
    """Figures out if a GitHub user is a known bot or not"""
    if "[bot]" in userid:  # Easiest way to detect is the [bot] marker
        return True
    # Try the bot file?
    known_robots = set()
    if os.path.isfile(KNOWN_BOTS_FILE):  # If we have a list file
        # Grab all lines that aren't comments
        bots_from_file = [x.strip() for x in open(KNOWN_BOTS_FILE).readlines() if not x.startswith("#")]
        #Update bot set with all non-empty lines
        known_robots.update([bot for bot in bots_from_file if bot])
    return userid in known_robots


class DiffComments:
    def __init__(self, uid, original_payload):
        self.created = time.time()
        self.diffs = []
        self.payload = original_payload

    def add(self, filename, diff, text):
        difftext = DIFF_COMMENT_BLURB % locals()
        self.diffs.append(difftext)


class Notifier:
    def __init__(self, cfg_file: str):
        self.config = yaml.safe_load(open(cfg_file))
        self.templates = {}
        self.diffcomments: typing.Dict[str, DiffComments] = {}
        for key, tmpl_file in self.config["templates"].items():
            if os.path.exists(tmpl_file):
                print("Loading template " + tmpl_file)
                subject, contents = open(tmpl_file).read().split("\n", 1)
                subject = subject.replace("subject: ", "")
                contents = contents.strip()
                self.templates[key] = (
                    subject,
                    contents,
                )

    def get_custom_subject(self, repository, action="catchall"):
        """Gets a subject template for a specific action, if specified via .asf.yaml"""
        # Rewrite some unintuitively named github actions to more human friendly ones.
        action_map = {
            "created_issue": "comment_issue",
            "created_pr": "comment_pr",
            "diffcomment_collated_pr": "diffcomment",
            "open_issue": "new_issue",
            "open_pr": "new_pr",
        }
        if action in action_map:
            action = action_map[action]

        ymlfile = f"/x1/asfyaml/ghsettings.{repository}.yml"  # Path to github settings yaml file
        if os.path.isfile(ymlfile):
            try:
                yml = yaml.safe_load(open(ymlfile))
            except yaml.parser.ParserError:  # Invalid YAML?!
                return
            custom_subjects = yml.get("custom_subjects")
            if custom_subjects and isinstance(custom_subjects, dict):
                if action in custom_subjects:
                    return custom_subjects[action]
                elif "catchall" in custom_subjects:  # If no custom subject exists for this action, but catchall does...
                    return custom_subjects["catchall"]

    def get_recipient(self, repository, itype, action="comment", userid=None):
        m = RE_PROJECT.match(repository)
        if m:
            project = m.group(1)
        else:
            project = "infra"
        repo_path = None
        scheme = {}
        for root_dir in self.config["repository_paths"]:
            for path in glob.glob(root_dir):
                if os.path.basename(path) == f"{repository}.git":
                    repo_path = path
                    break
        if repo_path:
            scheme_path = os.path.join(repo_path, self.config["scheme_file"])
            if os.path.exists(scheme_path):
                try:
                    scheme = yaml.safe_load(open(scheme_path))
                except Exception: # TODO: narrow further to expected Exceptions
                    pass

            # Check standard git config
            cfg_path = os.path.join(repo_path, "config")
            cfg = git.GitConfigParser(cfg_path)

            # If the yaml scheme is missing parts, weave in the defaults from the git config in their place
            # Commits mailing list
            if "commits" not in scheme:
                scheme["commits"] = cfg.get("hooks.asfgit", "recips") or self.config["default_recipient"]
            # Issues and Pull Requests
            if cfg.has_option("apache", "dev"):
                default_issue = cfg.get("apache", "dev")
                if "issues" not in scheme:
                    scheme["issues"] = default_issue
                if "pullrequests" not in scheme:
                    scheme["pullrequests"] = default_issue
            # Jira notification options
            if cfg.has_option("apache", "jira"):
                default_jira = cfg.get("apache", "jira")
                if "jira_options" not in scheme:
                    scheme["jira_options"] = default_jira

        if scheme:
            if itype not in ("commit", "jira") and userid:
                # Work out whether issue or pullrequest
                github_issue_type = itype == "issue" and "issues" or "pullrequests"

                # Work out the type of event (ticket status change, or comment)
                event_category = "unknown"
                if action in ("comment", "diffcomment", "diffcomment_collated", "edited", "deleted", "created"):
                    event_category = "comment"
                elif action in ("open", "close", "merge"):
                    event_category = "status"

                # Order of preference for scheme (most specific -> least specific)
                # Special rules that are only valid for bots like dependabot
                rule_order_bots = (
                    "{issue_type}_{event_category}_bot_{userid}",  # e.g. pullrequests_comment_bot_dependabot
                    "{issue_type}_bot_{userid}",  # e.g. pullrequests_bot_dependabot
                )
                # Humans (and bots with no bot-specific rules)
                rule_order_humans = (
                    "{issue_type}_{event_category}",  # e.g. pullrequests_comment
                    "{issue_type}",  # e.g. pullrequests
                )

                rule_dict = {
                    "issue_type": github_issue_type,
                    "event_category": event_category,
                    "userid": userid.replace("[bot]", ""),  # Only bot rules use this, so the bot tag is implied anyway.
                }

                # If bot, we remove the [bot] in the user ID and check the bot rules
                if is_bot(userid):
                    print(f"{userid} is a known bot, expanding rule-set")
                    for rule in rule_order_bots:
                        key = rule.format(**rule_dict)
                        if key in scheme and scheme[key]:  # If we have this scheme and it is non-empty, return it
                            return scheme[key]
                # Human rules (also applies to bots with no specific rules for them)
                for rule in rule_order_humans:
                    key = rule.format(**rule_dict)
                    if key in scheme and scheme[key]:  # If we have this scheme and it is non-empty, return it
                        return scheme[key]
                # return self.config["default_recipient"]  # No (non-empty) scheme found, return default git recipient

            elif itype == "commit" and "commits" in scheme:
                return scheme["commits"]
            elif itype == "jira":
                return scheme.get("jira_options", self.config["jira"]["default_options"])
        if itype == "jira":
            return self.config["jira"]["default_options"]
        return "dev@%s.apache.org" % project

    async def flush(self):
        to_remove = []
        for uid, diffcomment in self.diffcomments.items():
            if diffcomment.created < time.time() - DEFAULT_DIFF_WAIT:
                print(f"Writing collated diff with {len(diffcomment.diffs)} items...")
                payload = diffcomment.payload
                payload["diff"] = "\n\n".join(diffcomment.diffs)
                payload["action"] = "diffcomment_collated"
                await self.handle_payload({"payload": payload})
                to_remove.append(uid)
        for uid in to_remove:
            del self.diffcomments[uid]

    async def handle_payload(self, raw):
        payload = raw.get("payload")
        if not payload:  # Pong, use this for pushing collated items
            await self.flush()
            return
        user = payload.get("user")
        action = payload.get(
            "action"
        )  # open = new ticket, created = commented, edited = changed text, close = closed ticket, diffcomment = comment on file
        repository = payload.get("repo")
        if "only" in self.config and repository not in self.config["only"]:
            return
        if "ignore" in self.config and repository in self.config["ignore"]:
            return
        title = payload.get("title", "")
        text = payload.get("text", "")
        issue_id = payload.get("id", "")
        link = payload.get("link", "")
        filename = payload.get("filename", "")
        diff = payload.get("diff", "")
        pr_id = issue_id # Github uses the same number pool for PRs and issues
        category = payload.get("type") == "issue" and "issue" or "pr"
        node_id = payload.get("node_id")  # Used for message references/threading
        real_action = action + "_" + category
        if action == "diffcomment":
            uid = f"{repository}-{pr_id}-{user}"
            if uid not in self.diffcomments:
                self.diffcomments[uid] = DiffComments(uid, payload)
            self.diffcomments[uid].add(filename, diff, text)

        ml = self.get_recipient(repository, payload.get("type", "pullrequest"), action, user)
        print("notifying", ml)
        ml_list, ml_domain = ml.split("@", 1)
        if real_action in self.templates:
            # Note: the subjects are checked for validity in
            # https://github.com/apache/infrastructure-p6/blob/production/modules/gitbox/files/asfgit/package/asfyaml.py
            # See VALID_GITHUB_SUBJECT_VARIABLES and validate_github_subject()
            # The variable names listed in VALID_GITHUB_SUBJECT_VARIABLES must be defined
            # here as local variables
            subject_line = self.get_custom_subject(repository, real_action)  # Custom subject line?
            try:
                if subject_line:
                    subject_line = subject_line.format(**locals())
                else:
                    subject_line = self.templates[real_action][0] % locals()
                real_text = self.templates[real_action][1] % locals()
            except (KeyError, ValueError) as e:  # Template breakage can happen, ignore
                print(e)
                return
            msg_headers = {}
            msgid = "<%s-%s@gitbox.apache.org>" % (node_id, str(uuid.uuid4()))
            msgid_OP = "<%s@gitbox.apache.org>" % node_id
            if action == "open" and not payload.get("changes"):  # NB: If payload has a 'changes' element that is not None, it is NOT a new PR!
                msgid = msgid_OP  # This is the first email, make a deterministic message id
            else:
                msg_headers = {"In-Reply-To": msgid_OP}  # Thread from the first PR/issue email
            print(subject_line)
            # print(msgid)
            # print(msg_headers)
            if SEND_EMAIL:
                recipient = ml
                asfpy.messaging.mail(
                    sender=f"\"{user} (via GitHub)\" <git@apache.org>",
                    recipient=recipient,
                    subject=subject_line,
                    message=real_text,
                    messageid=msgid,
                    headers=msg_headers,
                )
            jopts = self.get_recipient(repository, "jira")
            if jopts:
                jira_text = real_text.split("-- ", 1)[0]
                self.notify_jira(jopts, pr_id, title, jira_text, link)

    async def listen(self):
        async for payload in asfpy.pubsub.listen(
            self.config["pubsub_url"], self.config.get("pubsub_user"), self.config.get("pubsub_pass")
        ):
            await self.handle_payload(payload)

    def jira_update_ticket(self, ticket, txt, worklog=False):
        """Post JIRA comment or worklog entry"""
        where = "comment"
        data = {"body": txt}
        if worklog:
            where = "worklog"
            data = {"timeSpent": "10m", "comment": txt}

        rv = requests.post(
            "https://issues.apache.org/jira/rest/api/latest/issue/%s/%s" % (ticket, where),
            headers=JIRA_HEADERS,
            json=data,
        )
        if rv.status_code == 200 or rv.status_code == 201:
            return "Updated JIRA Ticket %s" % ticket
        else:
            raise Exception(rv.text)

    def jira_remote_link(self, ticket, url, prno):
        """Post JIRA remote link to GitHub PR/Issue"""
        urlid = url.split("#")[0]  # Crop out anchor
        data = {
            "globalId": "github=%s" % urlid,
            "object": {
                "url": urlid,
                "title": "GitHub Pull Request #%s" % prno,
                "icon": {"url16x16": "https://github.com/favicon.ico"},
            },
        }
        rv = requests.post(
            "https://issues.apache.org/jira/rest/api/latest/issue/%s/remotelink" % ticket,
            headers=JIRA_HEADERS,
            json=data,
        )
        if rv.status_code == 200 or rv.status_code == 201:
            return "Updated JIRA Ticket %s" % ticket
        else:
            raise Exception(rv.text)

    def jira_add_label(self, ticket):
        """Add a "PR available" label to JIRA"""
        data = {"update": {"labels": [{"add": "pull-request-available"}]}}
        rv = requests.put(
            "https://issues.apache.org/jira/rest/api/latest/issue/%s" % ticket,
            headers=JIRA_HEADERS,
            json=data,
        )
        if rv.status_code == 200 or rv.status_code == 201:
            return "Added PR label to Ticket %s\n" % ticket
        else:
            raise Exception(rv.text)

    def notify_jira(self, jopts, prid, prtitle, prmessage, prlink):
        try:
            m = RE_JIRA_TICKET.search(prtitle)
            if m:
                jira_ticket = m.group(1)
                if "worklog" in jopts or "comment" in jopts:
                    print("[INFO] Adding comment to %s" % jira_ticket)
                    if not DEBUG:
                        self.jira_update_ticket(jira_ticket, prmessage, True if "worklog" in jopts else False)
                if "link" in jopts:
                    print("[INFO] Setting JIRA link for %s to %s" % (jira_ticket, prlink))
                    if not DEBUG:
                        self.jira_remote_link(jira_ticket, prlink, prid)
                if "label" in jopts:
                    print("[INFO] Setting JIRA label for %s" % jira_ticket)
                    if not DEBUG:
                        self.jira_add_label(jira_ticket)
        except Exception as e:
            print("[WARNING] Could not update JIRA: %s" % e)


def main():
    notifier = Notifier(CONFIG_FILE)
    asyncio.run(notifier.listen())


if __name__ == "__main__":
    main()
