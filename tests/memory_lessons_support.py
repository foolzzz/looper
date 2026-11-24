"""Synthetic journal-backed lessons for foundation tests."""

async def lesson(pool, project, statement):
    await pool.execute("INSERT INTO projects (id,name) VALUES ($1,$1) ON CONFLICT DO NOTHING", project)
    task = "task-" + statement
    await pool.execute("INSERT INTO tasks(id,requirement,project_id,status) VALUES ($1,'Synthetic task',$2,'completed')", task, project)
    journal = await pool.fetchval("INSERT INTO task_journal(task_id,step_id,kind,summary) "
                                  "VALUES ($1,'s1','step_accepted','Synthetic evidence') RETURNING id", task)
    number = await pool.fetchval("INSERT INTO memory_candidates(kind,source_kind,source_class,destination,state,"
        "reason,statement,fingerprint,project_id,valid_until) VALUES ('lesson','task','agent','looper','held',"
        "'shadow',$1,repeat('a',64),$2,now()+interval '90 days') RETURNING id", statement, project)
    await pool.execute("INSERT INTO memory_candidate_sources(candidate_id,kind,journal_id) VALUES ($1,'journal',$2)",
                       number, journal)
    return f"M-{number:04d}"
