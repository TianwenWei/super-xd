from super_xd.demo.tasks.database import Database
from super_xd.nlp.orchestration.tasks.runner import TaskRunner


def test_brief_reply_keeps_original_question_and_initial_vehicle_context(tmp_path):
    db=Database(tmp_path/'tasks.sqlite3')
    t=db.create_task('s','安排参观',context={'vehicle':{'windows':{'driver':40}}})
    db.save_task(t['id'],1,status='waiting_clarification',question='能接受步行十分钟吗？',summary='等你回复')
    updated=db.mutate_task(t['id'],'reply','可以',expected_revision=1)
    assert updated['question'] is None and updated['constraints']==['可以']
    assert updated['context']['clarification_history']==[{'question':'能接受步行十分钟吗？','answer':'可以','revision':2}]
    assert updated['context']['vehicle']['windows']['driver']==40
    # No real provider client is created or called by this test.
    runner=object.__new__(TaskRunner)
    prompt=runner._messages(updated)
    assert '能接受步行十分钟吗？' in prompt[-1]['content'] and '可以' in prompt[-1]['content']
