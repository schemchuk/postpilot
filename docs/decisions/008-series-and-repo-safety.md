# 008. Series identity and repository link checks

Status: ACCEPTED

Starting a series while another is active raises an error. Stopping or completing a series preserves
its history. A matching publication records the part, advances the series and marks the repository
as posted in one transaction. The publisher should pass the draft's series ID to distinguish a
publication from a previous run of the same repository and part number.

The link gate treats an API failure as a failed check and includes only public repositories with
both required files. Link cleanup matches the repository path case-insensitively, including links
to pages within that repository. Automated license creation is deferred.
