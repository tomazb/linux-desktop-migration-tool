# Selective integration from Codeberg upstream

This comparison was made against the read-only checkout of
[sesivany/linux-desktop-migration-tool](https://codeberg.org/sesivany/linux-desktop-migration-tool),
commit **[`7cc2443b50f710ea9a978d8391862cf764826bd6`](https://codeberg.org/sesivany/linux-desktop-migration-tool/commit/7cc2443b50f710ea9a978d8391862cf764826bd6)**,
dated January 5, 2026. It is a pinned source comparison, not a claim that this is
the current upstream HEAD. That commit merges upstream pull request
[#51](https://codeberg.org/sesivany/linux-desktop-migration-tool/pulls/51).

The integration selectively adapts useful newer behavior into this fork while
preserving existing fixes. It does not replace `migration.sh` wholesale or merge
all upstream behavior. The new Python workflow remains a separate, portable
Fedora XFCE 43 → 44 backup/restore path.

## Comparison and decisions

| Upstream feature or behavior | Decision | Result in this fork and reason |
| --- | --- | --- |
| Flatpak app full ref and origin, split user/system enumeration ([75482be](https://codeberg.org/sesivany/linux-desktop-migration-tool/commit/75482beee2034d86f6bb275b309ea8c0053cc639)) | Adapt | Legacy migration preserves complete ref, origin and installation scope. Python already captures the same identity in its manifest. Explicit identity prevents selecting an unrelated branch or remote. |
| Flatpak remote name/URL inventory ([68d3e94](https://codeberg.org/sesivany/linux-desktop-migration-tool/commit/68d3e94d7362a6e7947a3a99c1236b563f2d797a), [ba87776](https://codeberg.org/sesivany/linux-desktop-migration-tool/commit/ba87776547670b5a4e61826374dba8d9561e2a10)) | Adapt | Require a configured destination remote with the matching URL in the original scope. Report missing/changed remotes instead of implicitly trusting a newly copied source. |
| Automatic `remote-add --if-not-exists` | Defer | Neither workflow automatically creates remotes or copies signing trust. The user configures trusted remotes first; a remote name alone does not establish provenance. |
| Install system apps as user apps when privileges are absent | Defer | Preserve recorded scope and fail visibly when it cannot be used. Silent fallback changes availability and installation ownership. |
| Strip architecture from Flatpak refs | Defer | Keep the complete source ref. Python checks matching source/target architectures when package installation is requested; cross-architecture migration needs explicit reconciliation. |
| Discard the first Flatpak output row with `awk 'NR>1'` | Do not carry over | Nonterminal machine output must not lose its first app or remote. Keep every data row and validate the expected output fields. |
| Copy complete `.gnupg` instead of public-key export ([ba87776](https://codeberg.org/sesivany/linux-desktop-migration-tool/commit/ba87776547670b5a4e61826374dba8d9561e2a10)) | Adapt | Legacy migration privately stages persistent GnuPG files, excluding sockets/devices/locks; refuses a nonempty target keyring or symlink ancestors; never kills agents automatically. Python already supports `.gnupg` as an explicit secret selection. Preserve private keys/configuration, not only public keys, while avoiding an implicit destructive keyring merge. |
| Use `id -nG` for group membership ([0205f8b](https://codeberg.org/sesivany/linux-desktop-migration-tool/commit/0205f8b105e496d0b94fa3e1fdf292aad1d6e39e)) | Accept with exact checks | Legacy privilege checks compare complete group names so substrings are not treated as membership. Membership is an eligibility check, not a substitute for actual sudo authorization. |
| Earlier privilege discovery ([c4aa5ac](https://codeberg.org/sesivany/linux-desktop-migration-tool/commit/c4aa5acd65d3f5081e5d7c39e56205799edb80fc)) | Adapt | Scope decisions use privilege information without enabling automatic scope fallback. Python uses native package-manager/sudo confirmation instead of handling passwords itself. |
| Keyrings plus GNOME Online Accounts configuration in the pinned script | Adapt for Python | `.config/goa-1.0` joins the explicit secret selection alongside `.local/share/keyrings`. Accounts may still need reauthorization; copying files does not activate or authenticate them. |
| Desktop wallpaper preservation in the pinned script | Adapt for XFCE | Python selects `.local/share/backgrounds` by default with XFCE settings; it does not write GNOME dconf keys. Other wallpaper paths require explicit selection and path checks. |
| Directory-size visibility in the pinned script | Adapt | Python summaries show logical payload bytes, complementing file counts. This is not an estimate of filesystem overhead, free-space sufficiency or rollback size. |
| Toolbx/container and NetworkManager migration in the pinned script | Defer for Python | These require system/service-aware workflows. Python does not migrate container volumes, service databases or network profiles automatically. Legacy functionality remains separately scoped and live-unverified. |
| Replace this fork's shell script with the upstream file | Defer | Retain existing strict shell handling, dependency checks, SSH temporary-directory fixes, Toolbx command repair and SELinux relabeling. A wholesale replacement would discard those fixes and reintroduce unrelated differences. |

These decisions apply to the selected integration, not to an assertion that all
remaining legacy behavior is correct. The shell workflow and Python workflow have
different validation and recovery semantics. Both retain native Flatpak install
confirmations. Every source remote must already match the destination URL; used
remotes must be enabled. Custom named Flatpak installations are rejected rather
than silently omitted. In particular, only
the Python CLI supplies the bundle verification, preview and restore intent-plan
behavior documented in the [Fedora guide](FEDORA_43_TO_44.md).

## Provenance and licensing

The upstream project and original shell script are authored by **Jiří Eischmann**
(the source copyright comment uses “Jiri Eischmann”). The cited feature commits
are credited to him in upstream history. Preserve those source notices and the
existing [LICENSE](../LICENSE). The shell source states GNU GPL version 3 or, at
the user's option, any later version; this integration does not relicense it.
The license document remains unchanged.

The integration uses adapted behavior rather than claiming to have authored the
upstream features. Commit links above identify the reviewed source even if its
main branch later changes. This comparison records the source snapshot and
engineering decisions; it is not a new upstream release announcement.

## Verification and remaining work

Automated Python tests and shell syntax/stub tests can check parsing, scope,
command arguments, error propagation and preservation of fork fixes. They cannot
establish successful migration of a live SSH session, agent state, Flatpak
installation or desktop. Live Fedora 43/44 migration remains unverified for both
workflows; follow the [two-VM rehearsal](FEDORA_43_TO_44.md#two-vm-rehearsal) and test
legacy SSH behavior separately before relying on it.
