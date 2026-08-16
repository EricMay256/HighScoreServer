from app.vault.tables import vault_audit_events


def test_audit_events_correlate_to_write_requests_without_a_foreign_key() -> None:
    # An audit insert must never be able to fail on a referential constraint,
    # events for rejected or unauthenticated writes must keep their idempotency
    # key, and vault_write_requests must stay prunable. See vault ADR 0002.
    assert vault_audit_events.foreign_key_constraints == set()

    constraint_names = {
        constraint.name for constraint in vault_audit_events.constraints
    }
    assert "vault_audit_events_idempotency_principal_required" not in constraint_names


def test_audit_events_distinguish_targets_and_correlation_ids() -> None:
    assert {
        "principal_id",
        "target_type",
        "target_id",
        "idempotency_key",
        "request_id",
        "trace_id",
    } <= set(vault_audit_events.c.keys())

    constraint_names = {
        constraint.name for constraint in vault_audit_events.constraints
    }
    assert {
        "vault_audit_events_target_consistent",
        "vault_audit_events_idempotency_key_format",
    } <= constraint_names

    index_names = {index.name for index in vault_audit_events.indexes}
    assert {
        "idx_vault_audit_events_principal_occurred",
        "idx_vault_audit_events_request",
        "idx_vault_audit_events_trace",
        "idx_vault_audit_events_principal_idempotency",
    } == index_names


def test_latency_is_optional_so_lifecycle_events_need_no_fabricated_zero() -> None:
    assert vault_audit_events.c.latency_ms.nullable is True

    # The non-negative check tolerates NULL, so it still constrains real values.
    constraint_names = {
        constraint.name for constraint in vault_audit_events.constraints
    }
    assert "vault_audit_events_latency_nonnegative" in constraint_names
