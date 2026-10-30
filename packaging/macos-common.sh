#!/bin/bash
# portable配置の共通検査。取得値や絶対パスを診断へ出さない。
csr_error() { echo "$1" >&2; return 1; }
csr_safe_path() {
  local cursor="$1"
  case "$cursor" in /*) ;; *) csr_error '絶対パスで対象を指定してください。'; return 1;; esac
  while [ "$cursor" != '/' ]; do
    [ ! -L "$cursor" ] || { csr_error 'リンク経由の対象は操作しません。'; return 1; }
    cursor="$(dirname "$cursor")"
  done
}
csr_manifest() {
  local root="$1" canonical marker="${2:-$1/.codex-safe-resume-owner}"
  [ -d "$root" ] && [ ! -L "$root" ] || return 1
  canonical="$(cd "$root" && pwd -P)" || return 1
  (
    cd "$root" 2>/dev/null || exit 1
    # 標準Perlで全件を処理し、v1のリンクdigestにはreadlink出力の改行も含める。
    /usr/bin/perl -MDigest::SHA -MCwd=abs_path -e '
      use strict;
      use warnings;
      my ($root, $marker, $canonical) = @ARGV;
      binmode STDIN;
      binmode STDOUT;
      local $/ = "\0";
      while (defined(my $entry = <STDIN>)) {
        chomp $entry;
        (my $rel = $entry) =~ s{^\./}{};
        next if "$root/$rel" eq $marker;
        exit 1 if $rel =~ /[\n\r\t]/;
        if (-l $entry) {
          my $resolved = abs_path($entry);
          exit 1 unless defined($resolved) && index($resolved, "$canonical/") == 0;
          my $target = readlink($entry);
          exit 1 unless defined($target);
          print "L\t", Digest::SHA::sha256_hex($target . "\n"), "\t$rel\n";
        } elsif (-f $entry) {
          open(my $file, "<", $entry) or exit 1;
          binmode $file;
          my $sha = Digest::SHA->new(256);
          eval { $sha->addfile($file); 1 } or exit 1;
          close($file) or exit 1;
          print "F\t", $sha->hexdigest, "\t$rel\n";
        } elsif (-d $entry) {
          print "D\t-\t$rel\n";
        } else {
          exit 1;
        }
      }
    ' -- "$root" "$marker" "$canonical" < <(/usr/bin/find -P . -mindepth 1 -print0)
  ) | LC_ALL=C sort
}
csr_write_owner() {
  local root="$1" output marker="${2:-$1/.codex-safe-resume-owner}"
  [ ! -e "$marker" ] && [ ! -L "$marker" ] || return 1
  output="$(csr_manifest "$root" "$marker")" || return 1
  { printf 'codex-safe-resume-portable-v1\n'; printf '%s\n' "$output"; } > "$marker"
}
csr_verify_owner() {
  local root="$1" stored actual marker="${2:-$1/.codex-safe-resume-owner}"
  csr_safe_path "$root" || return 1
  [ -f "$marker" ] && [ ! -L "$marker" ] || { csr_error '配置の所有記録がありません。削除を停止しました。'; return 1; }
  stored="$(cat "$marker" 2>/dev/null)" || return 1
  actual="$(csr_manifest "$root" "$marker")" || return 1
  [ "$stored" = "$(printf 'codex-safe-resume-portable-v1\n%s' "$actual")" ] || { csr_error '配置後の変更を検出しました。削除を停止しました。'; return 1; }
}
csr_remove_settings() {
  local root="$1" helper="${BASH_SOURCE[0]}"
  case "$helper" in /*) ;; *) helper="$PWD/$helper" ;; esac
  csr_safe_path "$root" || return 1
  csr_safe_path "$helper" || return 1
  # Pythonのgui_leaseと同じsibling inodeを削除完了まで親プロセスで保持する。
  /usr/bin/perl -MFcntl=:DEFAULT,:flock,:mode -MCwd=abs_path -MFile::Basename=dirname,basename -e '
    use strict;
    use warnings;
    sub refuse { print STDERR "起動ロックを確認できないため設定を保持しました。\n"; exit 1; }
    my ($root, $helper) = @ARGV;
    my $parent = abs_path(dirname($root));
    refuse() unless defined($parent);
    my $name = basename($root);
    refuse() if $name eq "." || $name eq ".." || $name eq "/";
    $root = "$parent/$name";
    my @parent = lstat($parent);
    refuse() unless @parent && S_ISDIR($parent[2]) && $parent[4] == $< && !($parent[2] & 0022);
    my $lock = "$root.gui.lock";
    sysopen(my $lease, $lock, O_CREAT | O_RDWR | O_NOFOLLOW, 0600) or refuse();
    my @info = stat($lease);
    refuse() unless @info && S_ISREG($info[2]) && $info[3] == 1 && $info[4] == $<
      && !($info[2] & 0077) && $info[7] <= 1;
    unless (flock($lease, LOCK_EX | LOCK_NB)) {
      print STDERR "使用中または確認できない設定データを保持して停止しました。\n";
      exit 1;
    }
    my @path = lstat($lock);
    my @current = lstat($parent);
    refuse() unless @path && $path[0] == $info[0] && $path[1] == $info[1]
      && @current && $current[0] == $parent[0] && $current[1] == $parent[1];
    # 子の削除shellも同じopen-file descriptionを保持する。親の異常終了で排他を失わない。
    $^F = fileno($lease);
    fcntl($lease, F_SETFD, 0) or refuse();
    my $result = system "/bin/bash", "-c", q{source "$1"; _csr_remove_settings_locked "$2" "$3"},
      "csr-settings-removal", $helper, $root, fileno($lease);
    close($lease) or refuse();
    exit($result == 0 ? 0 : 1);
  ' -- "$root" "$helper"
}
# 内部専用。公開入口csr_remove_settingsがsibling lockを保持して呼び出す。
_csr_remove_settings_locked() {
  local root="$1" fd="${2:-}" entry name
  case "$fd" in ''|*[!0-9]*) csr_error '設定削除の起動ロックがありません。'; return 1;; esac
  # 直接呼出しでは削除しない。継承した同一inodeのfdで排他を再確認する。
  /usr/bin/perl -MFcntl=:flock,:mode -e '
    my ($path, $fd) = @ARGV;
    open(my $held, "+<&=", $fd) or exit 1;
    my @info = stat($held);
    my @path = lstat($path);
    exit 1 unless @info && @path && S_ISREG($info[2]) && $info[3] == 1
      && $info[4] == $< && !($info[2] & 0077) && $info[7] <= 1
      && $info[0] == $path[0] && $info[1] == $path[1];
    flock($held, LOCK_EX | LOCK_NB) or exit 1;
  ' -- "$root.gui.lock" "$fd" || { csr_error '設定削除の起動ロックを確認できません。'; return 1; }
  csr_safe_path "$root" || return 1
  [ -d "$root" ] || return 0
  for name in monitor.lock resume.sqlite3; do
    if [ -e "$root/$name" ] && /usr/sbin/lsof -t -- "$root/$name" >/dev/null 2>&1; then
      csr_error '使用中の設定データを保持して停止しました。'; return 1
    fi
  done
  while IFS= read -r -d '' entry; do
    name="${entry##*/}"
    case "$name" in resume.sqlite3|resume.sqlite3-wal|resume.sqlite3-shm|monitor.lock) ;; *) csr_error '未知の設定ファイルを保持しました。'; return 1;; esac
    [ -f "$entry" ] && [ ! -L "$entry" ] || { csr_error '想定外の設定ファイルを保持しました。'; return 1; }
  done < <(/usr/bin/find -P "$root" -mindepth 1 -maxdepth 1 -print0)
  while IFS= read -r -d '' entry; do rm -- "$entry" || return 1; done < <(/usr/bin/find -P "$root" -mindepth 1 -maxdepth 1 -print0)
  rmdir "$root"
}
