resource "aws_cloudwatch_log_group" "ecs" {
  name              = "/ecs/${local.name_prefix}"
  retention_in_days = 14
}

# -----------------------------------------------------------------------------
# Content Gate executor — structured event metrics.
#
# bubble-sync-eidarix.ts emits `[METRIC] <event> {…json…}` lines at the
# regression-prone code paths. Each filter below counts hits for one event so
# a rate drift (e.g. "topic_fallback_used" jumping after an agent prompt
# change) shows up as a CloudWatch time series before a user complains.
#
# Metrics land in namespace `NaicDashboard/BubbleSync` with a single
# dimension per event. Attach alarms via the console or a follow-up Terraform
# commit when baselines are known.
# -----------------------------------------------------------------------------

locals {
  bubble_sync_metrics = {
    sync_error                = "Content Gate executor reached the top-level catch — any > 0 is unusual."
    patch_failed              = "Bubble Data API PATCH returned non-OK — should never happen in steady state."
    agenda_item_not_found     = "Existing/Updated agenda item wasn't found by findAgendaItemByTitle."
    patch_fallback_to_create  = "UPDATE match_search returned nothing — executor fell back to CREATE (indicates URL/title drift)."
    topic_fallback_used       = "Agenda item create path fell onto the empty-topic chronicletopic (lookup failed for all topic names)."
    topic_not_found           = "Chronicle topic name failed to resolve after all five lookup probes."
    org_not_found             = "Organization name failed to resolve against the Bubble org table."
  }
}

resource "aws_cloudwatch_log_metric_filter" "bubble_sync_event" {
  for_each = local.bubble_sync_metrics

  name           = "${local.name_prefix}-bubble-sync-${each.key}"
  log_group_name = aws_cloudwatch_log_group.ecs.name
  # Match the exact tag emitted by smetric() in bubble-sync-eidarix.ts.
  pattern        = "\"[METRIC] ${each.key}\""

  metric_transformation {
    name          = each.key
    namespace     = "NaicDashboard/BubbleSync"
    value         = "1"
    default_value = "0"
  }
}
