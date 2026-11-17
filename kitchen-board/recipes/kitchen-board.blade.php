{{--
  Blade version of kitchen-board.liquid. Use it only if the Liquid version
  shows HTML tags as text instead of rendering them.
--}}
@php
  $src = $data['html'] ?? null;
  $device = $trmnl['device'] ?? [];
  $latest = $trmnl['sensors']['latest'] ?? [];

  $batt = (int) round($device['percent_charged'] ?? 0);
  $fill = (int) round($batt * 20 / 100);

  // LaraPaper reports Wi-Fi as bars, 0-3.
  $ws = (int) ($device['wifi_strength'] ?? 3);
  $wc = $ws >= 3 ? 'wifi-3' : ($ws == 2 ? 'wifi-2' : 'wifi-1');

  $tin = '--°F';
  if (!empty($latest['temperature']['value'])) {
      $t = $latest['temperature'];
      $isF = str_contains(strtolower($t['unit'] ?? ''), 'f');
      $tin = (int) round($isF ? $t['value'] : $t['value'] * 1.8 + 32) . '°F';
  }
  $hin = !empty($latest['humidity']['value']) ? (int) round($latest['humidity']['value']) . '%' : '--%';
@endphp
@if ($src)
{!! str_replace(
    ['%%BATTERY_FILL%%', '%%BATTERY%%', '%%WIFI_CLASS%%', '%%IN_TEMP%%', '%%IN_RH%%'],
    [$fill, $batt, $wc, $tin, $hin],
    $src
) !!}
@else
<div style="font: 22px/1.4 sans-serif; padding: 48px;">Calendar data isn't available. Check that the kitchen-board container is running and the polling URL is right.</div>
@endif
