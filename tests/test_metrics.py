from __future__ import annotations

from tunarr_autoscheduler.core.metrics import MetricsCollector


def test_render_prometheus_contains_core_metrics() -> None:
    metrics = MetricsCollector()
    metrics.set_active_generations(2)
    metrics.record_generation("ch1", "completed")
    metrics.record_pipeline_stage("ch1", "validator", 12.5, "success")
    metrics.record_media_sync(100.0, new_items=3, removed_items=1)
    metrics.record_upload("ch1", "success")
    metrics.record_validation_error("ch1", "dead_air")

    rendered = metrics.render_prometheus()

    assert "tunarr_active_generations 2" in rendered
    assert 'tunarr_generation_total{channel_id="ch1",status="completed"} 1' in rendered
    assert (
        'tunarr_pipeline_stage_total{channel_id="ch1",stage="validator",status="success"} 1'
        in rendered
    )
    assert (
        'tunarr_pipeline_stage_duration_ms_sum{channel_id="ch1",stage="validator"} 12.500'
        in rendered
    )
    assert (
        'tunarr_pipeline_stage_duration_ms_avg{channel_id="ch1",stage="validator"} 12.500'
        in rendered
    )
    assert 'tunarr_media_sync_items_total{type="new"} 3' in rendered
    assert 'tunarr_upload_total{channel_id="ch1",status="success"} 1' in rendered
    assert (
        'tunarr_validation_errors_total{channel_id="ch1",error_type="dead_air"} 1'
        in rendered
    )
