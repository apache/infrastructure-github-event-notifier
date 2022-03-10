# infrastructure-github-event-notifier
Apache Infra GitHub Event Notification Service

This service runs in the background and pulls in GitHub activity from our pubsub service.
new/closed/merged issues/prs and code/issue comments are emailed to the appropriate mailing list.
Where a code review from a user contains several comments on different pieces of code, the 
comments are collated into a single email.

The plan is to also add JIRA support later, for adding links and labels to JIRA tickets.
