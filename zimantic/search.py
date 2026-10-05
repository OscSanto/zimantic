"""Search every built ZIM index at once: meaning (FAISS), title keywords (SQLite FTS),
and full-text keywords (the Kiwix index inside the ZIM)."""
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote

import faiss
from libzim.search import Query, Searcher
from libzim.reader import Archive


class Search:
    def __init__(self, cfg: dict, embedder):
        self.cfg = cfg
        self.embedder = embedder
        self.indexes = {}  # zim name -> (sqlite connection, faiss index, archive, Kiwix full-text searcher)

        # establish sqlite conn to `astronomy.sqlite` containing: title, 1st para, paths, redirect targets, FTS5 title index.
        #FAISS index, conn, libzim's cluster cache should be preserved (file in OS cache (cached vs dry run: ~10ms vs 165ms for reading header AND ~5m vs 40ms for Searcher + query)
        for db_path in sorted(Path(cfg["index_dir"]).glob("*.sqlite")):
            # check_same_thread -> server's worker threads share db connection, with LOCK: in server.py making them take turns
            db = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False)
            if not db.execute("SELECT 1 FROM meta WHERE key = 'done'").fetchone():
                continue  # still building

            faissIndex = faiss.read_index(str(db_path.with_suffix(".faiss")), faiss.IO_FLAG_MMAP_IFC | faiss.IO_FLAG_READ_ONLY)
            if hasattr(faissIndex, "nprobe"): # on flat: no "nprobe", only IV indexes have nprobe
                faissIndex.nprobe = cfg["nprobe"]

            zim_path = Path(cfg["zim_dir"]) / f"{db_path.stem}.zim"
            zimArchive = Archive(str(zim_path)) if zim_path.exists() else None # libzim file handler. reads header and directory pointers
            searcher = Searcher(zimArchive) if zimArchive and zimArchive.has_fulltext_index else None #Xapian full text list for the ZIM
            
            self.indexes[db_path.stem] = (db, faissIndex, zimArchive, searcher) 

    def search(self, query: str, zim: list[str] | None = None, limit: int = 20) -> list[dict]:
        names = []
        if not zim: # user returned None or empty from dropdown -> fetch all  
            names.extend(self.indexes) # indexed zims   
        else: 
            for name in zim:
                if name in self.indexes: # skip zim's not indexed. 
                    names.append(name)
        
        extrafetch = 2 * limit  #redirects collapse several rows, so fetch extra
        queryVector = self.embedder.embed([f"query: {query}"])
        titleWords = " ".join(f'"{w}"' for w in re.findall(r"\w+", query))  

        semantic, keyword, fulltext = [], [], []

        for name in names:
            db, faissIndex, zimArchive, searcher = self.indexes[name]

            #if IVF: self.nprobe internally. 
            similarityScore, ids = faissIndex.search(queryVector, extrafetch) # euclidean inner product normalized, row IDs of nearest aritcles
            
            for s, i in zip(similarityScore[0], ids[0]):
                if i >= 0: #Faiss returns real matches only, if real matches < extrafetch -> fill leftover with ID: -1 & score: -1
                    semantic.append((float(s), name, int(i)))

            if titleWords:
                # ranking formula bm25 is the scorer with rarer words being valued higher 
                # lower bm25 score = better 
                TITLE_SEARCH = "SELECT rowid, bm25(docs) FROM docs WHERE docs MATCH ? ORDER BY rank LIMIT ?"
                for rowid, bm25 in db.execute(TITLE_SEARCH, (titleWords, extrafetch)):
                    keyword.append((bm25, name, rowid))

            if searcher:
                paths = searcher.search(Query().set_query(query)).getResults(0, extrafetch)
                fulltext += [(rank, name, zimArchive.get_entry_by_path(p)._index) for rank, p in enumerate(paths)]

        semantic.sort(reverse=True)  # higher similarity is better
        keyword.sort()               # lower bm25 is better
        fulltext.sort()              # Kiwix gives no comparable scores: interleave each ZIM's #1, #2, ...

        #####################################################
        # FUSION: 
        #    - Semantic, keyword, full-text
        #    - These lists have different valued scoring systems. (cosine 0-1, BM25 -neg, kiwix positioning). 
        # RRF uses only their ranking positions:
        #    - Being found by serveral lists matter most. Each list adds weight/(60 + ranking); weight is 1,
        #      except the full-text list on long queries (see fulltextWeight).
        #    - Article near top on all 3 lists beats article that's #1 on only one list.  
        # Long queries: (10+ words) 
        #    - Long queries are usually a remembered or pasted sentenced 
        #    - Kiwix's full-text search performs the best on exact wordings, thus its list weights are adjusted to 2x. 
        # In testing this took sentences from deep inside a page from 28% -> 62% (WikiMed) and
        # 11-45% -> 69-81% (StackExchange), with questions, titles and other languages unchanged. 
        #####################################################
        if len(re.findall(r"\w+", query)) >= self.cfg.get("long_query", 10):
            fulltextWeight = 2
        else:
            fulltextWeight = 1

        docs, score = {}, {}
        for ranked, weight in [(semantic, 1), (keyword, 1), (fulltext, fulltextWeight)]:
            pages = {}
            for _, name, rowid in ranked[:extrafetch]:
                doc = self._articleRow(name, rowid)
                if doc:
                    pages.setdefault((name, doc["path"]), doc)
            for rank, (key, doc) in enumerate(pages.items()):
                docs.setdefault(key, doc)
                score[key] = score.get(key, 0) + weight / (60 + rank)
        top = sorted(score, key=score.get, reverse=True)[:limit]
        for key in top:
            docs[key]["score"] = score[key]


        return [docs[key] for key in top]

    def _articleRow(self, name: str, rowid: int) -> dict | None:
        db = self.indexes[name][0]

        rowInfo = "SELECT title, lead, path, target FROM docs WHERE rowid = ?"
        row = db.execute(rowInfo, (rowid,)).fetchone()

        # real article: target is NULL
        # redirect: target holds row ID of artcle it points to
        if row and row[3] is not None: # Grab actual article
            row = db.execute(rowInfo, (row[3],)).fetchone()
        if not row:
            return None
        
        title, lead, path, _target = row

        # serve kiwix clickable title.
        url = f"{self.cfg['kiwix_url'].rstrip('/')}/{quote(name)}/{quote(path)}"
        return {"title": title, "lead": lead, "zim": name, "path": path, "url": url, "score": 0.0}
