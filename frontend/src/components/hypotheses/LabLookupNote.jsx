// The note beside a hypothesis's Find labs button: how many stored lab
// results are not yet in its evidence, how many suggested tests have no
// result on file, or that the queue is still checking.
export default function LabLookupNote({ lookups, id }) {
  const ready = lookups.ready[id];
  if (ready?.labs > 0) {
    return (
      <span className="rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] font-semibold text-primary">
        {ready.labs} to review
      </span>
    );
  }
  if (ready?.not_on_file > 0) {
    return <span className="text-[10px] text-muted-foreground">{ready.not_on_file} not on file</span>;
  }
  if (lookups.running && lookups.pending.includes(id)) {
    return <span className="text-[10px] text-muted-foreground">Checking labs…</span>;
  }
  return null;
}
