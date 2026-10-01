-- 0.15.0: what a model call's server-side loop did, and whether the call cost more than its worst case. iterations:
-- how many times the API sampled the model in the call (usage.iterations; NULL when the answer doesn't say).
-- overrun: the call cost more than its worst-case estimate; its cycle makes no more calls of its purpose. Calls from
-- before keep NULL and 0 (a finalized call can't change: llm_calls_finalize_once still guards both).
ALTER TABLE llm_calls ADD COLUMN iterations INTEGER CHECK (iterations IS NULL OR iterations >= 0);
ALTER TABLE llm_calls ADD COLUMN overrun INTEGER NOT NULL DEFAULT 0 CHECK (overrun IN (0, 1));
