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
import yaml
import os
import uuid
import git
import re
import time
import typing

CONFIG_FILE = "notifier.yaml"
SEND_EMAIL = False
RE_PROJECT = re.compile(r"(?:incubator-)?([^-]+)")
RE_JIRA_TICKET = re.compile(r"\b([A-Z0-9]+-\d+)\b")
DEFAULT_DIFF_WAIT = 10
DIFF_COMMENT_BLURB = """
##########
%(filename)s:
##########
%(diff)s

Review Comment:
%(text)s
"""


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

    def get_recipient(self, repository, itype, action="comment"):
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
                except:
                    pass

            # Check standard git config
            cfg_path = os.path.join(repo_path, "config")
            cfg = git.GitConfigParser(cfg_path)
            if not "commits" in scheme:
                scheme["commits"] = (
                    cfg.get("hooks.asfgit", "recips")
                    or self.config["default_recipient"]
                )
            if cfg.has_option("apache", "dev"):
                default_issue = cfg.get("apache", "dev")
                if not "issues" in scheme:
                    scheme["issues"] = default_issue
                if not "pullrequests" in scheme:
                    scheme["pullrequests"] = default_issue
            if cfg.has_option("apache", "jira"):
                default_jira = cfg.get("apache", "jira")
                if not "jira_options" in scheme:
                    scheme["jira_options"] = default_jira

        if scheme:
            if itype not in ["commit", "jira"]:
                it = "issues" if itype == "issue" else "pullrequests"
                if action in ["comment", "diffcomment", "edited", "deleted", "created"]:
                    if ("%s_comment" % it) in scheme:
                        return scheme["%s_comment" % it]
                    elif it in scheme:
                        return scheme.get(it, self.config["default_recipient"])
                elif action in ["open", "close", "merge"]:
                    if ("%s_status" % it) in scheme:
                        return scheme["%s_status" % it]
                    elif it in scheme:
                        return scheme.get(it, self.config["default_recipient"])
            elif itype == "commit" and "commits" in scheme:
                return scheme["commits"]
            elif itype == "jira":
                return scheme.get(
                    "jira_options", self.config["jira"]["default_options"]
                )
        if itype == "jira":
            return self.config["jira"]["default_options"]
        return "dev@%s.apache.org" % project

    def flush(self):
        to_remove = []
        for uid, diffcomment in self.diffcomments.items():
            if diffcomment.created < time.time() - DEFAULT_DIFF_WAIT:
                print(f"Writing collated diff with {len(diffcomment.diffs)} items...")
                payload = diffcomment.payload
                payload["diff"] = "\n\n".join(diffcomment.diffs)
                payload["action"] = "diffcomment_collated"
                self.handle_payload({"payload": payload})
                to_remove.append(uid)
        for uid in to_remove:
            del self.diffcomments[uid]

    def handle_payload(self, raw):
        payload = raw.get("payload")
        if not payload:  # Pong, use this for pushing collated items
            self.flush()
            return
        user = payload.get("user")
        action = payload.get(
            "action"
        )  # open = new ticket, created = commented, edited = changed text, close = closed ticket, diffcomment = comment on file
        repository = payload.get("repo")
        if "only" in self.config and repository not in self.config["only"]:
            return
        title = payload.get("title", "")
        text = payload.get("text", "")
        issue_id = payload.get("id", "")
        link = payload.get("link", "")
        filename = payload.get("filename", "")
        diff = payload.get("diff", "")
        pr_id = issue_id
        node_id = payload.get("node_id")  # Used for message references/threading
        real_action = (
            action + "_" + (payload.get("type") == "issue" and "issue" or "pr")
        )
        if action == "diffcomment":
            uid = f"{repository}-{pr_id}-{user}"
            if uid not in self.diffcomments:
                self.diffcomments[uid] = DiffComments(uid, payload)
            self.diffcomments[uid].add(filename, diff, text)

        ml = self.get_recipient(repository, payload.get("type"), action)
        ml_list, ml_domain = ml.split("@", 1)
        if real_action in self.templates:
            try:
                real_subject = self.templates[real_action][0] % locals()
                real_text = self.templates[real_action][1] % locals()
            except (KeyError, ValueError) as e:  # Template breakage can happen, ignore
                print(e)
                return
            msg_headers = {}
            msgid = "<%s-%s@gitbox.apache.org>" % (node_id, str(uuid.uuid4()))
            msgid_OP = "<%s@gitbox.apache.org>" % node_id
            if action == "open":
                msgid = (
                    msgid_OP  # This is the first email, make a deterministic message id
                )
            else:
                msg_headers = {
                    "In-Reply-To": msgid_OP
                }  # Thread from the first PR/issue email
            print(real_subject)
            if real_action == "diffcomment_collated_pr":
                print(real_text)
            # print(msgid)
            # print(msg_headers)
            if SEND_EMAIL:  # NOT YET!
                recipient = self.config["default_recipient"]
                asfpy.messaging.mail(
                    sender="GitBox <git@apache.org>",
                    recipient=recipient,
                    subject=real_subject,
                    message=real_text,
                    messageid=msgid,
                    headers=msg_headers,
                )

    def listen(self):
        listener = asfpy.pubsub.Listener(self.config["pubsub_url"])
        listener.attach(self.handle_payload, raw=True)


def main():
    notifier = Notifier(CONFIG_FILE)
    notifier.listen()


if __name__ == "__main__":
    main()
