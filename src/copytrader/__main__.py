import argparse
from dataclasses import asdict
from datetime import timedelta
import json
from pathlib import Path
from collections import Counter
from .parser import parse_message
from .paper import Config, PaperEngine, Quote, instant


def dump(path,obj):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    Path(path).write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def main():
    ap=argparse.ArgumentParser(description='Offline paper-only parser and replay. No live execution.')
    sub=ap.add_subparsers(dest='command',required=True)
    p=sub.add_parser('parse');p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p=sub.add_parser('replay');p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--db',type=Path,required=True);p.add_argument('--config',type=Path);p.add_argument('--quotes',type=Path)
    p=sub.add_parser('demo');p.add_argument('--output-dir',type=Path,required=True)
    a=ap.parse_args()
    if a.command=='demo':return demo(a.output_dir)
    # Reject malformed input before writing any output or ledger.
    records=[json.loads(line) for line in a.input.read_text(encoding='utf-8').splitlines() if line.strip()]
    if a.command=='parse':
        parsed=[parse_message(r).as_dict() for r in records]
        dump(a.output,dict(parser_version='0.1.0',messages=len(parsed),types=dict(Counter(r['type'] for r in parsed)),leg_statuses=dict(Counter(l['status'] for r in parsed for l in r['legs'])),records=parsed))
    else:
        config=Config(**json.loads(a.config.read_text())) if a.config else Config()
        quotes=[Quote(**q) for q in json.loads(a.quotes.read_text())] if a.quotes else []
        for q in quotes:q.validate()
        engine=PaperEngine(a.db,config)
        try:
            ordered=sorted(records,key=lambda r:instant(r['date']))
            events=[];now=None
            for r in ordered:
                now=instant(r['date'])
                available=[q for q in quotes if 0<=(now-instant(q.observed_at)).total_seconds()<=config.max_quote_age_sec]
                engine.advance(now,available)
                events.append(engine.process(r,now,available))
            report=dict(kind='offline_replay',supplied_quote_snapshots=len(quotes),warning='Quotes are caller-supplied fixtures, not verified platform history. Without quotes no fills are invented. Replay clock uses original message time; this is not live performance.',events=events)
            if now:report['ledger']=engine.snapshot(now)
            dump(a.output,report)
        finally:engine.close()
    print(str(a.output))


def demo(output):
    output.mkdir(parents=True,exist_ok=True)
    now=instant('2026-11-17T02:00:00Z')
    record=dict(channel='SYNTHETIC_DEMO',message_id=1,date=now.isoformat(),text='⚽ Demo League: Alpha vs Beta\n🟢 Moneyline: ALP (+100%)\nhttps://www.foregate.com/en/event/999999999999999999',links=[])
    signal=parse_message(record);leg=signal.legs[0]
    quote=Quote('synthetic-1',signal.event_id,'Alpha','Beta',leg.identity(),'synthetic-market',now.isoformat(),'0.50','2','0.02','0.50','SYNTHETIC_DEMO_NOT_MARKET_DATA')
    # In-memory ledger ensures a rerun cannot overwrite or double replay an old demo DB.
    engine=PaperEngine(':memory:')
    try:
        events=[engine.process(record,now,[quote])]
        states=[engine.snapshot(now)]
        engine.advance(now,[quote]);states.append(engine.snapshot(now))
        later=now+timedelta(seconds=61)
        engine.advance(later);states.append(engine.snapshot(later))
        events.append(engine.process(record,later,[quote]))
        engine.settle(1,'0',later,'SYNTHETIC_LOSS_FIXTURE')
        states.append(engine.snapshot(later))
        dump(output/'demo.json',dict(kind='synthetic_paper_demo',warning='Fully synthetic signal, quote, market ID and outcome; no historical earnings evidence.',events=events,states=states))
        print('离线合成演示：2 份部分成交 → 同一报价不重复成交 → 到期释放剩余预占 → 重复信号被拒绝 → 合成结算。')
        print('该演示不连接平台，不代表历史收益。结果：'+str(output/'demo.json'))
    finally:engine.close()

if __name__=='__main__':main()
