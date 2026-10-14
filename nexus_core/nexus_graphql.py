#!/usr/bin/env python3
"""Minimal synchronous Nexus Mods GraphQL v2 client used by auto-correlation.

Only read operations live here. Account mutations remain on the stable REST v1 client in
``nexus_account``. GraphQL failures raise ``NexusGraphQLError`` so the correlation pipeline
can degrade a tier without losing local results.
"""
from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from typing import Any, Iterable

GQL_URL = "https://api.nexusmods.com/v2/graphql"
GAME_ID_MAP = {"cyberpunk2077": 3333}
APP_NAME = "Nexus-Collection-Manager"
APP_VERSION = "0.17.6"


class NexusGraphQLError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, errors: Any = None):
        super().__init__(message)
        self.status = status
        self.errors = errors


MOD_FIELDS = """
    uid modId name summary version author createdAt updatedAt
    endorsements downloads pictureUrl category status
"""

REQ_FIELDS = """
    modRequirements {
      nexusRequirements(offset: 0, count: 50) {
        nodes { id modId modName url notes externalRequirement gameId }
      }
      modsRequiringThisMod(offset: 0, count: 50) {
        nodes { id modId modName url notes externalRequirement gameId }
      }
      dlcRequirements { gameExpansion { id name } notes }
    }
"""


class NexusGraphQLClient:
    def __init__(self, api_key: str, *, timeout: int = 45):
        if not api_key:
            raise NexusGraphQLError("A Nexus Mods API key is required for correlation.")
        self.api_key = api_key
        self.timeout = timeout

    def execute(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        req = urllib.request.Request(
            GQL_URL,
            data=body,
            method="POST",
            headers={
                "APIKEY": self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Application-Name": APP_NAME,
                "Application-Version": APP_VERSION,
                "User-Agent": f"{APP_NAME}/{APP_VERSION}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8", errors="replace"))
            except Exception:  # noqa: BLE001
                payload = None
            raise NexusGraphQLError(
                f"Nexus GraphQL HTTP {exc.code}: {exc.reason}", status=exc.code, errors=payload
            ) from exc
        except urllib.error.URLError as exc:
            raise NexusGraphQLError(f"Nexus GraphQL connection failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise NexusGraphQLError("Nexus GraphQL request timed out.") from exc
        except (ValueError, TypeError) as exc:
            raise NexusGraphQLError(f"Invalid Nexus GraphQL response: {exc}") from exc

        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors:
            msgs = "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errors)
            raise NexusGraphQLError(f"Nexus GraphQL errors: {msgs}", errors=errors)
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, dict) else {}

    @staticmethod
    def game_id(domain: str) -> int:
        try:
            return GAME_ID_MAP[domain]
        except KeyError as exc:
            raise NexusGraphQLError(f"Unknown Nexus game domain: {domain}") from exc

    def batch_file_hashes(self, md5s: Iterable[str]) -> list[dict[str, Any]]:
        unique = list(dict.fromkeys(x.lower() for x in md5s if x))
        hits: list[dict[str, Any]] = []
        for offset in range(0, len(unique), 500):
            chunk = unique[offset:offset + 500]
            if not chunk:
                continue
            query = """
            query BatchFileHashes($md5s: [String!]!) {
              fileHashes(md5s: $md5s) {
                md5 fileName fileSize gameId modFileId
                modFile {
                  fileId name version categoryId category size date
                  mod { uid modId name summary version author createdAt updatedAt
                        endorsements downloads pictureUrl category status }
                }
              }
            }
            """
            rows = self.execute(query, {"md5s": chunk}).get("fileHashes") or []
            hits.extend(row for row in rows if isinstance(row, dict))
        return hits

    def batch_mods(self, domain: str, mod_ids: Iterable[int | str], *, requirements: bool = True) -> dict[int, dict]:
        ids = []
        for raw in mod_ids:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value > 0 and value not in ids:
                ids.append(value)
        gid = self.game_id(domain)
        out: dict[int, dict] = {}
        # Alias queries are intentionally kept small because requirements are expensive.
        limit = 10 if requirements else 25
        req = REQ_FIELDS if requirements else ""
        for offset in range(0, len(ids), limit):
            chunk = ids[offset:offset + limit]
            aliases = "\n".join(
                f"m_{mid}: mod(modId: {mid}, gameId: {gid}) {{ {MOD_FIELDS} {req} }}"
                for mid in chunk
            )
            data = self.execute(f"query BatchMods {{ {aliases} }}")
            for mid in chunk:
                row = data.get(f"m_{mid}")
                if isinstance(row, dict):
                    out[mid] = row
        return out

    def batch_mods_basic(self, domain: str, mod_ids: Iterable[int | str]) -> dict[int, dict]:
        """High-throughput basic metadata lookup using legacyModsByDomain."""
        ids = []
        for raw in mod_ids:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value > 0 and value not in ids:
                ids.append(value)
        out: dict[int, dict] = {}
        for offset in range(0, len(ids), 25):
            chunk = ids[offset:offset + 25]
            if not chunk:
                continue
            query = f"""
            query BatchModsByDomain($ids: [CompositeDomainWithIdInput!]!) {{
              legacyModsByDomain(ids: $ids) {{ nodes {{ {MOD_FIELDS} }} }}
            }}
            """
            variables = {"ids": [{"gameDomain": domain, "modId": mid} for mid in chunk]}
            block = self.execute(query, variables).get("legacyModsByDomain") or {}
            for row in block.get("nodes") or []:
                if isinstance(row, dict) and row.get("modId") is not None:
                    try:
                        out[int(row["modId"])] = row
                    except (TypeError, ValueError):
                        pass
        return out

    def get_mod(self, domain: str, mod_id: int | str, *, requirements: bool = True) -> dict[str, Any]:
        gid = self.game_id(domain)
        req = REQ_FIELDS if requirements else ""
        query = f"""
        query GetMod($modId: ID!, $gameId: ID!) {{
          mod(modId: $modId, gameId: $gameId) {{ {MOD_FIELDS} {req} }}
        }}
        """
        row = self.execute(query, {"modId": int(mod_id), "gameId": gid}).get("mod")
        return row if isinstance(row, dict) else {}

    def search_mods(self, domain: str, name: str, *, count: int = 20) -> list[dict[str, Any]]:
        gid = self.game_id(domain)
        query = f"""
        query SearchMods($filter: ModsFilter!, $count: Int!, $sort: [ModsSort!]) {{
          mods(filter: $filter, count: $count, sort: $sort) {{
            nodes {{ {MOD_FIELDS} }}
          }}
        }}
        """
        variables = {
            "filter": {
                "gameId": [{"value": gid, "op": "EQUALS"}],
                "name": [{"value": f"*{name}*", "op": "WILDCARD"}],
            },
            "count": max(1, min(int(count), 50)),
            "sort": [{"field": "endorsements", "direction": "DESC"}],
        }
        block = self.execute(query, variables).get("mods") or {{}}
        return [row for row in (block.get("nodes") or []) if isinstance(row, dict)]

    def search_file_contents(
        self,
        domain: str,
        *,
        file_path_wildcard: str | None = None,
        file_extension: str | None = None,
        count: int = 20,
    ) -> list[dict[str, Any]]:
        gid = self.game_id(domain)
        filt: dict[str, Any] = {"gameId": [{"value": gid, "op": "EQUALS"}]}
        if file_path_wildcard:
            filt["filePathWildcard"] = [{"value": file_path_wildcard, "op": "WILDCARD"}]
        if file_extension:
            filt["fileExtensionExact"] = [{"value": file_extension, "op": "EQUALS"}]
        query = """
        query SearchFileContents($filter: ModFileContentSearchFilter!, $count: Int!) {
          modFileContents(filter: $filter, count: $count) {
            nodes { filePath fileName fileSize modId fileId }
          }
        }
        """
        block = self.execute(query, {"filter": filt, "count": max(1, min(int(count), 50))}).get("modFileContents") or {}
        return [row for row in (block.get("nodes") or []) if isinstance(row, dict)]

    def collection_latest(self, slug: str, domain: str) -> dict[str, Any]:
        """Return the latest published collection revision and its Nexus mod rows."""
        query = """
        query GetCollection($slug: String!) {
          collection(slug: $slug) {
            slug name summary endorsements totalDownloads
            latestPublishedRevision {
              revisionNumber
              modFiles {
                optional fileId
                file { fileId name version size uri mod { modId name summary author version
                       endorsements downloads pictureUrl status } }
              }
            }
          }
        }
        """
        col = self.execute(query, {"slug": slug}).get("collection")
        if not isinstance(col, dict):
            return {}
        return col
