# 競輪場単位の原子的公開 — 検証・移行・ロールバック手順

状態: **2026-11-21に追加DDLと互換Edge Function v10を本番へ適用済み**。旧日単位RPCと旧ワークフローは残してあり、新ワークフローを戻せば即時に旧方式へ切り替えられる。

## 公開境界

`(dataset_family, race_date, venue_code)` を公開単位とする。1場の新世代は、その場の期待レース集合、全レース本体、各レースの選手ID・車番、保存ハッシュ、出典時刻、ライン退行を検証した後だけ公開する。公開は `cloud_active_venue_generations` の1行を切り替え、同じトランザクション内で `cloud_active_day_indexes` を再構成する。

したがって、A場が12/12件でB場が11/12件なら、A場だけ新世代へ切り替わり、B場は旧世代（旧世代がなければ未公開）のままになる。日インデックスは各場の有効ポインタだけを列挙し、存在しない世代を参照しない。

## バックアップと保持方針

1. Supabaseの通常バックアップに加え、アプリ側では次を削除せず保持する。
   - `public.shared_snapshots`
   - `public.cloud_ingest_audit`
   - 既存の `cloud_*` 関数、RLS、権限
   - 新方式を導入済みなら `cloud_venue_generation_*`、`cloud_active_*`、`cloud_venue_publication_audit`
2. 新方式は既存の `shared_snapshots` と旧 `cloud_cas_write` を削除・改名しない。公開時は新旧読取互換データを同一トランザクションで更新する。
3. 各競輪場の旧世代は `cloud_venue_generation_records` と `cloud_venue_generations` に残す。監査表の `previous_generation_id` から対象場だけ戻せる。
4. データベース全体の時点復元は他の本番更新を失う可能性があるため、通常の切戻しには使わない。競輪場ポインタまたは旧公開ジョブを使う。

## 適用済み検証

1. `database/venue_generation_publication.sql` は本番へ1トランザクションで追加適用した。
2. `anon` / `authenticated` がテーブルと書き込みRPCを実行できず、サーバー側の限定ロールだけが実行できることを確認する。
3. 保存済み日付から競輪場世代を作り、ステージだけ行う。現行 `shared_snapshots` と現行読取経路は変更しない。
4. 各場について、期待レース集合と保存レース集合が完全一致する世代だけを公開する。2026-11-18の混在データを一括で「完全」と再ラベルしない。
5. 新旧の読取結果を比較する。日インデックス、全レースID、選手登録番号、車番、ライン、コメント対象集合、結果・払戻しのrace_id参照が一致することを確認する。
6. ローカル保存データで失敗注入、再実行、並列実行、旧世代保持を検証した。実DBでの同時実行競合と本番ジョブの初回完走は切替後に確認する。

## 読取切替

読取側は、最初に `cloud_read_active_day_index` を読み、そのインデックスに記載された各場について `cloud_read_active_venue_records` を読む。世代IDが日インデックスと一致しない応答は表示せず、再読込する。旧 `shared_snapshots` と新経路を混在させない。

コメントは `build_catalog_from_active_venues` により同じ日インデックスからrace_idと登録番号を得られる。現在のローカル保存ファイル由来コメント処理は変更しない。結果・払戻しは独立したrecord typeのため、今回のポインタ切替対象外だが、race_id集合の突合は移行ゲートに含める。

## ロールバック

### データ公開だけ戻す

1. 対象場の現在revisionと直前のgeneration_idを監査表から確認する。
2. 直前世代が完全に保持されていることを再検証する。
3. `cloud_publish_venue_generation(..., p_expected_revision => 現在revision, p_action => 'rollback')` でポインタを戻す。
4. 同一トランザクションで日インデックスrevisionが増え、対象場だけ旧generation_idへ戻ったことを確認する。

### アプリ読取経路を戻す

1. GitHub Actionsを `cloud_publication=legacy` で手動実行するか、ワークフローを直前コミットへrevertする。
2. `database/rollback_venue_generation_publication.sql` を適用し、新しいstage/publish RPCのサーバー実行権限を剥奪する。
3. 旧 `shared_snapshots`、旧 `cloud_cas_write`、Edge Functionの旧read/write処理は残るため、日単位ジョブを継続できる。
4. 新テーブルは削除しない。原因調査と再切替が終わるまで、ステージ済み世代・監査・日インデックスを保持する。

### DDLを撤去する

読取と書込が旧方式へ完全に戻り、バックアップ復元試験が済み、明示承認がある場合だけ行う。最初に関数権限を剥奪し、依存を列挙し、監査・世代テーブルを再バックアップしてから削除する。自動DROPやCASなしのポインタ更新は行わない。

## 停止条件

バックアップ未検証、期待レース集合が不明、同一世代IDに異なるデータ、source時刻の逆行、確定ラインの退行、revision競合、日インデックス不一致のいずれかがあれば、その場の公開だけを停止する。他場の既存公開は維持する。
