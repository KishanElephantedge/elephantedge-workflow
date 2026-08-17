"""Market -> Account Intelligence -- Batch 11. The controlled bridge between Content
Intelligence (market/topic level) and account-level evidence (Company/GtmSignal.company_id).

    ContentTopic (market level)
        |
        +-- ContentTopicEvidence -- links to --> GtmSignal
                                                        |
                                                        +-- company_id (when the source signal
                                                            genuinely carries one)
                                                                |
                                                                v
                                                        Company (account level)

A market topic is NEVER, by itself, evidence that a specific company is affected (Part 2's own
explicit boundary). The only defensible relationship this module recognizes is Case B from the
spec: the SAME GtmSignal row is simultaneously (a) linked to a company via its own `company_id`
column, and (b) linked to a ContentTopic via ContentTopicEvidence. That's not an inference -- it's
the same, already-real observation viewed from two angles, so no new evidence or judgment is
introduced, only a join over two already-existing relationships."""
