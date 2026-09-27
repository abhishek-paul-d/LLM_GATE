from release_gate.generator import generate_cases, validate_cases
from release_gate.generator.schema import VARIANTS, SuiteConfig
from release_gate.generator.validate import cross_split_similarity


def test_image_pull_error_family_generates_valid_grounded_cases() -> None:
    config = SuiteConfig(
        suite_version="probe",
        seed=7,
        families=["image_pull_error", "memory_pressure", "bad_config_rollout", "upstream_dependency"],
        variants=list(VARIANTS),
        cases_per_cell=3,
        release_templates=["image_pull_error/b", "memory_pressure/b", "bad_config_rollout/b", "upstream_dependency/b"],
    )
    cases = generate_cases(config)

    assert validate_cases(cases) == []
    assert cross_split_similarity(cases)[0][0] < 0.7
    clear_cases = [case for case in cases if case.family == "image_pull_error" and case.variant == "clear"]
    assert clear_cases
    assert all(case.expected.category == "configuration" for case in clear_cases)
    for template in ("a", "b"):
        template_cases = [case for case in clear_cases if case.scenario_id == f"image_pull_error/{template}"]
        assert template_cases
        for case in template_cases:
            key_facts = case.expected.required_facts[1]
            assert any(fact.casefold() in case.input_text.casefold() for fact in key_facts)
