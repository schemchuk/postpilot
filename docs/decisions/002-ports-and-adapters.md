# 002. Ports and adapters, Channel protocol

Status: PROPOSED

Context: channels must be pluggable and nothing in the core may depend on a network (AGENTS.md).
Decision: core defines `Channel` (`validate`, `publish`) and `Source` protocols; each network lives in
`channels/<name>.py`. The core never imports an SDK. Adding a network = one adapter plus tests.
Consequence: a shared contract-test suite runs against every adapter and a fake channel.
