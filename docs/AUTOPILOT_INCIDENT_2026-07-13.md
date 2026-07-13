# Autopilot long-job incident — 2026-07-13

## Esito

Tre job reali sullo stesso monorepo sono terminati senza completare il goal.
Il codice prodotto dai worker non era necessariamente errato: diversi tentativi
validi sono stati scartati dal runner prima dell'integrazione. L'incidente e'
stato ricostruito confrontando chat, righe SQLite di job/task/run, processi,
worktree, Git e output strutturati dei provider. Il repository applicativo non
e' stato modificato durante la diagnosi.

## Cause verificate

1. Gli scope `directory/**` e `*pattern*` erano trattati come prefissi letterali,
   generando falsi `scope-violation`.
2. I comandi reali pnpm filtrati, script Node, unittest con `PYTHONPATH`,
   `py_compile` e controlli Git erano marcati `descriptive`; il gate concludeva
   quindi `validation-failed` senza eseguirli.
3. I worktree isolati non vedevano gli alberi ignorati `node_modules`/venv gia'
   presenti nel checkout sorgente.
4. Gli hook dei worker effimeri aggiornavano il percorso canonico del workspace
   e aggiungevano sessioni duplicate alla timeline.
5. La creazione del job riutilizzava il task attivo: job distinti condividevano
   checkpoint e cronologia.
6. Un crash dopo il claim poteva riportare un task a `ready` quando aveva gia'
   consumato il massimo numero di tentativi.
7. `stop` terminava il runner ma non riconciliava necessariamente i process group
   dei provider e le righe run rimaste `running`.
8. Il processo detached scriveva soltanto il report finale; un job interrotto
   lasciava quindi un log vuoto.

## Correzioni

- matching glob esplicito con distinzione tra path valido e vera violazione;
- parser allow-listed senza shell per le prove repository-native osservate;
- symlink locali ai soli alberi dipendenza ignorati nei worktree;
- colonna additiva schema-2-compatible con `source_path` immutabile e task
  unico per ogni job, senza interrompere processi MCP 0.5.0 gia' aperti;
- protezione del workspace canonico e soppressione hook nei worker;
- hard gate pre-claim sui tentativi, valido anche dopo crash e in parallelo;
- reaping dei run provider durante `stop` e finalizzazione `interrupted`;
- milestone JSON redatte e flushed durante tutto il ciclo;
- launcher installato che preferisce il binario Memory Hub del relativo HOME.

## Prove di non regressione

La suite copre migrazione v1→v2, backfill additivo e downgrade compatibile del
release candidate v3, due job nello stesso progetto, worktree
effimero con la stessa remote, comandi osservati nell'incidente, scope glob,
dipendenze, hook no-op, crash al limite tentativi, due task falliti in parallelo,
runner e provider reali `sleep` terminati da `stop`, milestone e fast-forward
end-to-end. Restano proibiti `git push`, publish/release/deploy, Python `-c`,
variabili ambiente arbitrarie e path traversal.
