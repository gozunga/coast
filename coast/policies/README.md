# coast/policies - policy-in-code - M1

One module per resource returning `DocumentedRuleDefault` lists;
`__init__.list_rules()` aggregates them for the `oslo.policy.policies` entry
point. Rule table: `doc/source/api.md#policies`, notes: `etc/coast/POLICY.md`.
