-- 0.15.0: old texts are pruned. Only the event log was pruned, so the tool calls' inputs and results and the model's
-- replies grew without limit (3.3 MB in 3 days). After db.TEXT_DAYS Ember's code sets them to '[pruned]'
-- (db.prune_texts). The rows stay, and the model calls (their costs, tokens and purpose) are never touched. The guards
-- that kept this history from changing now allow that one change and nothing else: a finished tool call's input to
-- '{}' and its result to '[pruned]', the rest of its row as it was; a call's text to '[pruned]', without its stop
-- details. Nothing can be deleted, and no text can become another.
DROP TRIGGER tool_calls_finalize_once;
CREATE TRIGGER tool_calls_finalize_once BEFORE UPDATE ON tool_calls
WHEN (OLD.status <> 'started' OR NEW.cycle_id IS NOT OLD.cycle_id OR NEW.llm_call_id IS NOT OLD.llm_call_id
    OR NEW.seq IS NOT OLD.seq OR NEW.tool IS NOT OLD.tool OR NEW.input IS NOT OLD.input
    OR NEW.started_at IS NOT OLD.started_at OR NEW.parent_id IS NOT OLD.parent_id)
    AND NOT (OLD.status <> 'started' AND NEW.input = '{}' AND NEW.result = '[pruned]'
        AND NEW.id IS OLD.id AND NEW.cycle_id IS OLD.cycle_id AND NEW.llm_call_id IS OLD.llm_call_id
        AND NEW.parent_id IS OLD.parent_id AND NEW.seq IS OLD.seq AND NEW.phase IS OLD.phase
        AND NEW.origin IS OLD.origin AND NEW.tool IS OLD.tool AND NEW.tool_use_id IS OLD.tool_use_id
        AND NEW.project_id IS OLD.project_id AND NEW.status IS OLD.status AND NEW.started_at IS OLD.started_at
        AND NEW.finished_at IS OLD.finished_at AND NEW.summary IS OLD.summary)
BEGIN
    SELECT RAISE(ABORT, 'tool_calls: a finished tool call cannot change');
END;
DROP TRIGGER call_texts_no_update;
CREATE TRIGGER call_texts_no_update BEFORE UPDATE ON call_texts
WHEN NOT (NEW.llm_call_id IS OLD.llm_call_id AND NEW.text = '[pruned]' AND NEW.stop_details IS NULL)
BEGIN SELECT RAISE(ABORT, 'call_texts: history cannot change'); END;
