"""Original tiny corpus demonstrating real immutable release storage."""
import argparse,hashlib,json,tempfile
from pathlib import Path
from literary.tools import alignment as a, alignment_v2 as v2, alignment_release as releases

def sha(data):return hashlib.sha256(data).hexdigest()
def write(root,relative,data):
    p=root/relative;p.parent.mkdir(parents=True,exist_ok=True)
    p.write_bytes(data);return p
def json_bytes(obj):return (json.dumps(obj,ensure_ascii=False,sort_keys=True)+'\n').encode()
def jsonl(rows):return b''.join(json_bytes(row) for row in rows)

def fixture(root):
    root=Path(root).resolve();volume='Volume One'
    bodies=['A red lantern stood beside the closed gate.',
            'The keeper wrote a date on the empty ledger.',
            'At dawn the courier returned the borrowed key.']
    lines=[volume];chapters=[];chunks=[]
    for n,body in enumerate(bodies,1):
        chapter=f'ch{n:04}';chunk=f'{chapter}_ck0001';title=f'Chapter {n}'
        lines.extend([title,body]);start=len(lines)-1
        chapters.append(dict(chapter_id=chapter,chapter_number=n,title=title,volume_title=volume,
            source_line_title=start,source_line_body_start=start+1,source_line_end=start+1,
            char_count=len(body),paragraph_count=1))
        path=f'chunks/{chunk}.json';digest=sha(body.encode())
        write(root,path,json_bytes(dict(chunk_id=chunk,text=body,text_sha256=digest)))
        chunks.append(dict(chunk_id=chunk,chapter_id=chapter,chunk_index=1,volume_title=volume,path=path,text_sha256=digest))
    source=('\n'.join(lines)+'\n').encode();cb=jsonl(chapters);kb=jsonl(chunks)
    write(root,'source.txt',source);write(root,'chapters.jsonl',cb);write(root,'chunks.jsonl',kb)
    manifest=dict(schema_version=2,source=dict(path='source.txt',sha256=sha(source),size_bytes=len(source),bom=False,encoding='utf-8',line_count=len(lines),read_only=False),
        topology=dict(chapter_index_path='chapters.jsonl',chapter_index_sha256=sha(cb),chunk_index_path='chunks.jsonl',chunk_index_sha256=sha(kb),chapter_count=3,chunk_count=3,volume_count=1,chapter_body_char_count=sum(map(len,bodies)),body_paragraph_count=3),
        volume_markers=[dict(volume_title=volume,source_line=1,literal_text=volume,sha256=sha(volume.encode()))],
        volume_ranges=[dict(volume_title=volume,first_chapter_id='ch0001',last_chapter_id='ch0003',chapter_count=3)],
        batching=dict(max_chapters=3,max_chapter_body_chars=1000,expected_batch_count=1,observed_max_batch_chars=sum(map(len,bodies))),
        artifact_paths=dict(generated_reading_text='literary/curation/alignment/reading_projection.txt'))
    write(root,'literary/curation/alignment/manifest.json',json_bytes(manifest))
    context=a.load_context(root);a.write_batch_index(context)
    write(root,'literary/curation/alignment/issues.jsonl',b'')
    write(root,'literary/curation/alignment/patches.jsonl',b'')
    a.generate_reading_projection(context)
    return context

def run(root):
    context=fixture(root);v2.checkpoint_legacy(context)
    view=releases.baseline(context);store=releases.Store(context.alignment_root)
    digest=store.save(view);loaded=store.load(context,digest)
    assert loaded.digest==digest and not loaded.terminal_ids
    return dict(directory=str(root),release_sha256=digest,chapters=len(context.chapters),reviewed_chapters=len(loaded.terminal_ids),status='unreviewed structural checkpoint; not literary acceptance')

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path);args=p.parse_args()
    if args.out:
        args.out.mkdir(parents=True,exist_ok=False);print(json.dumps(run(args.out),indent=2))
    else:
        with tempfile.TemporaryDirectory(prefix='alignment-demo-') as root:print(json.dumps(run(Path(root)),indent=2))
if __name__=='__main__':main()
