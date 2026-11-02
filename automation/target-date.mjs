const SCHEDULE_TIMES = new Map([
  ['9 0 * * *', {hour: 9, minute: 9}],
  ['0 9 * * *', {hour: 18, minute: 0}],
]);

const JST_PARTS = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Asia/Tokyo',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
});

function previousDate(date) {
  const value = new Date(`${date}T00:00:00Z`);
  value.setUTCDate(value.getUTCDate() - 1);
  return value.toISOString().slice(0, 10);
}

export function resolveTargetDate({now = new Date(), schedule} = {}) {
  const instant = new Date(now);
  if (Number.isNaN(instant.getTime())) throw Error('現在日時が不正です');

  const parts = Object.fromEntries(
    JST_PARTS.formatToParts(instant)
      .filter(({type}) => type !== 'literal')
      .map(({type, value}) => [type, value]),
  );
  const date = `${parts.year}-${parts.month}-${parts.day}`;
  if (!schedule) return date;

  const scheduled = SCHEDULE_TIMES.get(schedule);
  if (!scheduled) throw Error(`未対応の定期実行時刻です: ${schedule}`);

  const currentMinutes = Number(parts.hour) * 60 + Number(parts.minute);
  const scheduledMinutes = scheduled.hour * 60 + scheduled.minute;

  return currentMinutes < scheduledMinutes ? previousDate(date) : date;
}
