package llm.model_tiers_parity_test

import rego.v1

import data.llm.authz
import data.llm.allow_model

# authz.rego and allow_model.rego each maintain a copy of model_tiers.
# This test enforces they remain identical: any divergence silently widens or
# narrows access in one policy without affecting the other.
test_model_tiers_are_identical if {
    authz.model_tiers == allow_model.model_tiers
}
