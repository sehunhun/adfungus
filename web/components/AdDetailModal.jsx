import { useState, useRef, useEffect } from 'react';
import { fmtDate, daysRunning, fmtMetric } from '../lib/utils';

const METRIC_SERIES = [
  { key: 'view_count', label: '조회수', color: '#4f6ee8' },
  { key: 'like_count', label: '좋아요', color: '#c54763' },
  { key: 'comments_count', label: '댓글', color: '#56b28c' },
];

function startOfWeek(date) {
  const result = new Date(date);
  const day = result.getDay();
  const diff = day === 0 ? -6 : 1 - day;
  result.setDate(result.getDate() + diff);
  result.setHours(0, 0, 0, 0);
  return result;
}

function formatBucketLabel(date, granularity) {
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${month}.${day}`;
}

function buildMetricBuckets(history, granularity) {
  const grouped = new Map();

  for (const item of history || []) {
    const createdAt = item?.created_at ? new Date(item.created_at) : null;
    if (!createdAt || Number.isNaN(createdAt.getTime())) continue;

    const bucketDate = granularity === 'daily'
      ? new Date(createdAt.getFullYear(), createdAt.getMonth(), createdAt.getDate())
      : startOfWeek(createdAt);
    const bucketKey = bucketDate.toISOString();
    const current = grouped.get(bucketKey);

    if (!current || new Date(current.created_at) < createdAt) {
      grouped.set(bucketKey, {
        label: formatBucketLabel(bucketDate, granularity),
        created_at: createdAt.toISOString(),
        view_count: Number(item.view_count || 0),
        like_count: Number(item.like_count || 0),
        comments_count: Number(item.comments_count || 0),
      });
    }
  }

  return Array.from(grouped.values()).sort((a, b) => new Date(a.created_at) - new Date(b.created_at));
}

function MetricTrendChart({ history }) {
  const [granularity, setGranularity] = useState('daily');
  const points = buildMetricBuckets(history, granularity);

  if (!points.length) {
    return (
      <div style={{ padding: '72px 24px', textAlign: 'center', color: 'var(--muted)', fontSize: '14px' }}>
        반응 지표 데이터가 아직 없습니다.
      </div>
    );
  }

  const chartWidth = 720;
  const chartHeight = 320;
  const padding = { top: 24, right: 18, bottom: 48, left: 56 };
  const innerWidth = chartWidth - padding.left - padding.right;
  const innerHeight = chartHeight - padding.top - padding.bottom;
  const maxValue = Math.max(
    1,
    ...points.flatMap((point) => METRIC_SERIES.map((series) => Number(point[series.key] || 0))),
  );
  const uniqueYTicks = [...new Set([0, 0.25, 0.5, 0.75, 1].map((ratio) => Math.round(maxValue * ratio)))].sort((a, b) => a - b);

  const xAt = (index) => padding.left + (points.length === 1 ? innerWidth / 2 : (innerWidth * index) / (points.length - 1));
  const yAt = (value) => padding.top + innerHeight - (innerHeight * value) / maxValue;

  return (
    <div style={{ display: 'grid', gap: '18px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '16px', flexWrap: 'wrap' }}>
        <h4 style={{ margin: 0, fontSize: '15px', color: 'var(--navy)' }}>기간별 반응 추이</h4>
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
          <div style={{ display: 'inline-flex', padding: '3px', borderRadius: '999px', background: '#eef2ff', border: '1px solid #dbe2ff' }}>
            <button
              onClick={() => setGranularity('weekly')}
              style={{ border: 0, borderRadius: '999px', padding: '8px 14px', fontSize: '13px', fontWeight: 700, background: granularity === 'weekly' ? '#16213d' : 'transparent', color: granularity === 'weekly' ? '#fff' : '#42526b', cursor: 'pointer' }}
            >
              주간
            </button>
            <button
              onClick={() => setGranularity('daily')}
              style={{ border: 0, borderRadius: '999px', padding: '8px 14px', fontSize: '13px', fontWeight: 700, background: granularity === 'daily' ? '#16213d' : 'transparent', color: granularity === 'daily' ? '#fff' : '#42526b', cursor: 'pointer' }}
            >
              월간
            </button>
          </div>
          <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', justifyContent: 'flex-end' }}>
            {METRIC_SERIES.map((series) => (
              <div
                key={series.key}
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: '6px',
                  padding: '7px 12px',
                  borderRadius: '999px',
                  background: '#fff',
                  border: '1px solid #e6ebf2',
                  fontSize: '13px',
                  fontWeight: 700,
                  color: '#334155',
                }}
              >
                <span style={{ width: '8px', height: '8px', borderRadius: '999px', background: series.color }} />
                {series.label}
              </div>
            ))}
          </div>
        </div>
      </div>

      <div style={{ border: '1px solid #dbe2ea', borderRadius: '18px', background: 'linear-gradient(180deg, #ffffff 0%, #fbfdff 100%)', padding: '18px 18px 10px', boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.8)' }}>
        <svg viewBox={`0 0 ${chartWidth} ${chartHeight}`} style={{ width: '100%', height: 'auto', display: 'block' }}>
          {[0, 0.25, 0.5, 0.75, 1].map((ratio) => {
            const y = padding.top + innerHeight - innerHeight * ratio;
            return (
              <line
                key={ratio}
                x1={padding.left}
                y1={y}
                x2={chartWidth - padding.right}
                y2={y}
                stroke={ratio === 0 ? '#c8d3df' : '#edf2f7'}
                strokeWidth={ratio === 0 ? 1.5 : 1}
              />
            );
          })}

          {points.map((point, index) => (
            <text
              key={point.created_at}
              x={xAt(index)}
              y={chartHeight - 14}
              textAnchor="middle"
              fontSize="12"
              fontWeight="700"
              fill="#64748b"
            >
              {point.label}
            </text>
          ))}

          {METRIC_SERIES.map((series) => {
            const linePoints = points.map((point, index) => `${xAt(index)},${yAt(Number(point[series.key] || 0))}`).join(' ');
            return (
              <g key={series.key}>
                <polyline
                  fill="none"
                  stroke={series.color}
                  strokeWidth="3"
                  strokeLinejoin="round"
                  strokeLinecap="round"
                  points={linePoints}
                />
                {points.map((point, index) => (
                  <g key={`${series.key}-${point.created_at}`}>
                    <circle cx={xAt(index)} cy={yAt(Number(point[series.key] || 0))} r="5" fill="#fff" stroke={series.color} strokeWidth="3" />
                  </g>
                ))}
              </g>
            );
          })}
        </svg>
      </div>
    </div>
  );
}

function MetricTrendChartV2({ history }) {
  const [granularity, setGranularity] = useState('weekly');
  const points = buildMetricBuckets(history, granularity === 'daily' ? 'daily' : 'weekly');

  if (!points.length) {
    return (
      <div style={{ padding: '72px 24px', textAlign: 'center', color: 'var(--muted)', fontSize: '14px' }}>
        반응 지표 데이터가 아직 없습니다.
      </div>
    );
  }

  const chartWidth = 720;
  const chartHeight = 320;
  const padding = { top: 24, right: 18, bottom: 48, left: 56 };
  const innerWidth = chartWidth - padding.left - padding.right;
  const innerHeight = chartHeight - padding.top - padding.bottom;
  const maxValue = Math.max(
    1,
    ...points.flatMap((point) => METRIC_SERIES.map((series) => Number(point[series.key] || 0))),
  );
  const uniqueYTicks = [...new Set([0, 0.25, 0.5, 0.75, 1].map((ratio) => Math.round(maxValue * ratio)))].sort((a, b) => a - b);
  const xAt = (index) => padding.left + (points.length === 1 ? innerWidth / 2 : (innerWidth * index) / (points.length - 1));
  const yAt = (value) => padding.top + innerHeight - (innerHeight * value) / maxValue;

  return (
    <div style={{ display: 'grid', gap: '18px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '16px', flexWrap: 'wrap' }}>
        <h4 style={{ margin: 0, fontSize: '15px', color: 'var(--navy)' }}>기간별 반응 추이</h4>
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
          <div style={{ display: 'inline-flex', padding: '3px', borderRadius: '999px', background: '#eef2ff', border: '1px solid #dbe2ff' }}>
            <button
              onClick={() => setGranularity('weekly')}
              style={{ border: 0, borderRadius: '999px', padding: '8px 14px', fontSize: '13px', fontWeight: 700, background: granularity === 'weekly' ? '#16213d' : 'transparent', color: granularity === 'weekly' ? '#fff' : '#42526b', cursor: 'pointer' }}
            >
              주간
            </button>
            <button
              onClick={() => setGranularity('daily')}
              style={{ border: 0, borderRadius: '999px', padding: '8px 14px', fontSize: '13px', fontWeight: 700, background: granularity === 'daily' ? '#16213d' : 'transparent', color: granularity === 'daily' ? '#fff' : '#42526b', cursor: 'pointer' }}
            >
              일간
            </button>
          </div>
          <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', justifyContent: 'flex-end' }}>
            {METRIC_SERIES.map((series) => (
              <div
                key={series.key}
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: '6px',
                  padding: '7px 12px',
                  borderRadius: '999px',
                  background: '#fff',
                  border: '1px solid #e6ebf2',
                  fontSize: '13px',
                  fontWeight: 700,
                  color: '#334155',
                }}
              >
                <span style={{ width: '8px', height: '8px', borderRadius: '999px', background: series.color }} />
                {series.key === 'view_count' ? '조회수' : series.key === 'like_count' ? '좋아요' : '댓글'}
              </div>
            ))}
          </div>
        </div>
      </div>

      <div style={{ border: '1px solid #dbe2ea', borderRadius: '18px', background: 'linear-gradient(180deg, #ffffff 0%, #fbfdff 100%)', padding: '18px 18px 10px', boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.8)' }}>
        <svg viewBox={`0 0 ${chartWidth} ${chartHeight}`} style={{ width: '100%', height: 'auto', display: 'block' }}>
          {uniqueYTicks.map((tickValue) => {
            const y = yAt(tickValue);
            return (
              <g key={tickValue}>
                <line
                  x1={padding.left}
                  y1={y}
                  x2={chartWidth - padding.right}
                  y2={y}
                  stroke={tickValue === 0 ? '#c8d3df' : '#dbe5f0'}
                  strokeWidth={tickValue === 0 ? 1.5 : 1}
                  strokeDasharray={tickValue === 0 ? undefined : '4 6'}
                />
                <text x={padding.left - 10} y={y + 4} textAnchor="end" fontSize="11" fontWeight="700" fill="#7b8794">
                  {tickValue.toLocaleString('ko-KR')}
                </text>
              </g>
            );
          })}

          {points.map((point, index) => (
            <text key={point.created_at} x={xAt(index)} y={chartHeight - 14} textAnchor="middle" fontSize="12" fontWeight="700" fill="#64748b">
              {point.label}
            </text>
          ))}

          {METRIC_SERIES.map((series) => {
            const linePoints = points.map((point, index) => `${xAt(index)},${yAt(Number(point[series.key] || 0))}`).join(' ');
            return (
              <g key={series.key}>
                <polyline fill="none" stroke={series.color} strokeWidth="3" strokeLinejoin="round" strokeLinecap="round" points={linePoints} />
                {points.map((point, index) => (
                  <g key={`${series.key}-${point.created_at}`}>
                    <circle cx={xAt(index)} cy={yAt(Number(point[series.key] || 0))} r="5" fill="#fff" stroke={series.color} strokeWidth="3" />
                    <title>{`${series.key === 'view_count' ? '조회수' : series.key === 'like_count' ? '좋아요' : '댓글'}: ${Number(point[series.key] || 0).toLocaleString('ko-KR')}`}</title>
                  </g>
                ))}
              </g>
            );
          })}
        </svg>
      </div>
    </div>
  );
}

const METRIC_SERIES_UI = [
  { key: 'view_count', label: '조회수', color: '#4f6ee8' },
  { key: 'like_count', label: '좋아요', color: '#c54763' },
  { key: 'comments_count', label: '댓글', color: '#56b28c' },
];

function formatMetricNumber(value) {
  return Number(value || 0).toLocaleString('ko-KR');
}

function formatDeltaAmount(delta) {
  if (delta === null || delta === undefined) return null;
  if (delta === 0) return '0';
  return `${delta > 0 ? '+' : ''}${formatMetricNumber(delta)}`;
}

function formatDeltaRate(current, previous) {
  if (previous === null || previous === undefined) return null;
  if (previous === 0) {
    if (current === 0) return '0%';
    return 'New';
  }
  const rate = ((current - previous) / previous) * 100;
  const absRate = Math.abs(rate);
  const formatted = absRate >= 10 ? absRate.toFixed(0) : absRate.toFixed(1);
  return `${rate > 0 ? '+' : rate < 0 ? '-' : ''}${formatted}%`;
}

function metricDeltaColor(delta) {
  if (delta > 0) return '#d94164';
  if (delta < 0) return '#2f6fe4';
  return '#64748b';
}

function MetricTrendChartInteractive({ history }) {
  const [granularity, setGranularity] = useState('daily');
  const [selectedKey, setSelectedKey] = useState(METRIC_SERIES_UI[0].key);
  const [hoveredPoint, setHoveredPoint] = useState(null);
  const points = buildMetricBuckets(history, granularity === 'daily' ? 'daily' : 'weekly');

  if (!points.length) {
    return (
      <div style={{ padding: '72px 24px', textAlign: 'center', color: 'var(--muted)', fontSize: '14px' }}>
        반응 지표 데이터가 아직 없습니다.
      </div>
    );
  }

  const chartWidth = 720;
  const chartHeight = 320;
  const padding = { top: 24, right: 20, bottom: 48, left: 72 };
  const innerWidth = chartWidth - padding.left - padding.right;
  const innerHeight = chartHeight - padding.top - padding.bottom;
  const selectedSeries = METRIC_SERIES_UI.find((series) => series.key === selectedKey) || METRIC_SERIES_UI[0];
  const xAt = (index) => padding.left + (points.length === 1 ? innerWidth / 2 : (innerWidth * index) / (points.length - 1));
  const seriesBounds = Object.fromEntries(
    METRIC_SERIES_UI.map((series) => {
      const values = points.map((point) => Number(point[series.key] || 0));
      const min = Math.min(...values);
      const max = Math.max(...values);
      return [series.key, { min, max }];
    }),
  );
  const yAt = (seriesKey, value) => {
    const bounds = seriesBounds[seriesKey];
    if (!bounds) return padding.top + innerHeight / 2;
    if (bounds.max === bounds.min) return padding.top + innerHeight / 2;
    const ratio = (value - bounds.min) / (bounds.max - bounds.min);
    return padding.top + innerHeight - innerHeight * ratio;
  };
  const selectedBounds = seriesBounds[selectedSeries.key] || { min: 0, max: 0 };
  const yTicks = selectedBounds.max === selectedBounds.min
    ? [selectedBounds.min]
    : [0, 0.25, 0.5, 0.75, 1].map((ratio) => (
      selectedBounds.min + (selectedBounds.max - selectedBounds.min) * ratio
    ));
  const hoveredValue = hoveredPoint ? Number(points[hoveredPoint.pointIndex]?.[selectedSeries.key] || 0) : null;
  const previousValue = hoveredPoint && hoveredPoint.pointIndex > 0
    ? Number(points[hoveredPoint.pointIndex - 1]?.[selectedSeries.key] || 0)
    : null;
  const deltaValue = hoveredPoint && previousValue !== null ? hoveredValue - previousValue : null;
  const hoverX = hoveredPoint ? xAt(hoveredPoint.pointIndex) : null;
  const hoverY = hoveredPoint ? yAt(selectedSeries.key, hoveredValue) : null;
  const tooltipWidth = 220;
  const tooltipHeight = 104;
  const tooltipLeft = hoveredPoint
    ? hoverX > chartWidth - padding.right - tooltipWidth - 24
      ? hoverX - tooltipWidth - 18
      : hoverX + 14
    : 0;
  const tooltipTop = hoveredPoint
    ? Math.max(20, Math.min(hoverY - 74, chartHeight - tooltipHeight - 12))
    : 0;

  const selectMetric = (metricKey) => {
    setHoveredPoint(null);
    setSelectedKey(metricKey);
  };

  return (
    <div style={{ display: 'grid', gap: '18px' }}>
      <h4 style={{ margin: 0, fontSize: '15px', color: 'var(--navy)' }}>기간별 반응 추이</h4>

      <div
        style={{
          position: 'relative',
          border: '1px solid #dbe2ea',
          borderRadius: '18px',
          background: 'linear-gradient(180deg, #ffffff 0%, #fbfdff 100%)',
          padding: '18px 18px 10px',
          boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.8)',
        }}
      >
        {hoveredPoint ? (
          <>
            <div
              style={{
                position: 'absolute',
                left: `${((hoverX - 18) / chartWidth) * 100}%`,
                top: 18,
                bottom: 58,
                width: '1px',
                background: 'rgba(79, 110, 232, 0.16)',
                pointerEvents: 'none',
              }}
            />
            <div
              style={{
                position: 'absolute',
                left: `${tooltipLeft}px`,
                top: `${tooltipTop + 18}px`,
                minWidth: '188px',
                maxWidth: `${tooltipWidth}px`,
                padding: '10px 14px',
                borderRadius: '18px',
                border: '1px solid #16213d',
                background: '#fff',
                boxShadow: '0 18px 30px rgba(15, 23, 42, 0.18)',
                pointerEvents: 'none',
                zIndex: 2,
              }}
            >
              <div style={{ fontSize: '12px', fontWeight: 700, color: '#64748b', marginBottom: '2px' }}>
                {points[hoveredPoint.pointIndex]?.label}
              </div>
              <div style={{ fontSize: '14px', fontWeight: 700, color: '#334155' }}>
                {selectedSeries.label}
              </div>
              <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: '10px', marginTop: '2px', whiteSpace: 'nowrap' }}>
                <strong style={{ fontSize: '24px', lineHeight: 1, color: '#0f172a', minWidth: 0 }}>
                  {formatMetricNumber(hoveredValue)}
                </strong>
                {deltaValue !== null ? (
                  <span style={{ fontSize: '18px', lineHeight: 1, fontWeight: 800, color: metricDeltaColor(deltaValue), flexShrink: 0 }}>
                    {formatDeltaRate(hoveredValue, previousValue)}
                  </span>
                ) : null}
              </div>
              {deltaValue !== null ? (
                <div style={{ marginTop: '4px', fontSize: '12px', fontWeight: 700, color: metricDeltaColor(deltaValue) }}>
                  전시점 대비 {formatDeltaAmount(deltaValue)}
                </div>
              ) : (
                <div style={{ marginTop: '4px', fontSize: '12px', fontWeight: 700, color: '#94a3b8' }}>
                  첫 수집 시점
                </div>
              )}
            </div>
          </>
        ) : null}

        <svg viewBox={`0 0 ${chartWidth} ${chartHeight}`} style={{ width: '100%', height: 'auto', display: 'block' }}>
          {yTicks.map((tickValue, index) => {
            const y = yAt(selectedSeries.key, tickValue);
            const isBaseline = index === 0;
            return (
              <g key={`${selectedSeries.key}-${tickValue}-${index}`}>
                <line
                  x1={padding.left}
                  y1={y}
                  x2={chartWidth - padding.right}
                  y2={y}
                  stroke={isBaseline ? '#c8d3df' : '#dbe5f0'}
                  strokeWidth={isBaseline ? 1.5 : 1}
                  strokeDasharray={isBaseline ? undefined : '4 6'}
                />
                <text x={padding.left - 10} y={y + 4} textAnchor="end" fontSize="11" fontWeight="700" fill="#7b8794">
                  {formatMetricNumber(Math.round(tickValue))}
                </text>
              </g>
            );
          })}

          {points.map((point, index) => (
            <text key={point.created_at} x={xAt(index)} y={chartHeight - 14} textAnchor="middle" fontSize="12" fontWeight="700" fill="#64748b">
              {point.label}
            </text>
          ))}

          <g key={selectedSeries.key}>
            <polyline
              fill="none"
              stroke={selectedSeries.color}
              strokeWidth="3"
              strokeLinejoin="round"
              strokeLinecap="round"
              points={points.map((point, index) => `${xAt(index)},${yAt(selectedSeries.key, Number(point[selectedSeries.key] || 0))}`).join(' ')}
            />
            {points.map((point, index) => {
              const pointValue = Number(point[selectedSeries.key] || 0);
              const isHovered = hoveredPoint?.pointIndex === index;
              return (
                <circle
                  key={`${selectedSeries.key}-${point.created_at}`}
                  cx={xAt(index)}
                  cy={yAt(selectedSeries.key, pointValue)}
                  r={isHovered ? '7' : '5'}
                  fill="#fff"
                  stroke={selectedSeries.color}
                  strokeWidth={isHovered ? '4' : '3'}
                  style={{ cursor: 'pointer' }}
                  onMouseEnter={() => setHoveredPoint({ pointIndex: index })}
                  onMouseLeave={() => setHoveredPoint((current) => (
                    current?.pointIndex === index ? null : current
                  ))}
                />
              );
            })}
          </g>
        </svg>
      </div>

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
        <div style={{ display: 'inline-flex', padding: '3px', borderRadius: '999px', background: '#eef2ff', border: '1px solid #dbe2ff' }}>
          <button
            onClick={() => setGranularity('weekly')}
            style={{ border: 0, borderRadius: '999px', padding: '8px 14px', fontSize: '13px', fontWeight: 700, background: granularity === 'weekly' ? '#16213d' : 'transparent', color: granularity === 'weekly' ? '#fff' : '#42526b', cursor: 'pointer' }}
          >
            주간
          </button>
          <button
            onClick={() => setGranularity('daily')}
            style={{ border: 0, borderRadius: '999px', padding: '8px 14px', fontSize: '13px', fontWeight: 700, background: granularity === 'daily' ? '#16213d' : 'transparent', color: granularity === 'daily' ? '#fff' : '#42526b', cursor: 'pointer' }}
          >
            일간
          </button>
        </div>

        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', justifyContent: 'flex-end', marginLeft: 'auto' }}>
          {METRIC_SERIES_UI.map((series) => {
            const isActive = selectedKey === series.key;
            return (
              <button
                key={series.key}
                type="button"
                onClick={() => selectMetric(series.key)}
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: '6px',
                  padding: '7px 12px',
                  borderRadius: '999px',
                  background: isActive ? '#fff' : '#f8fafc',
                  border: `1px solid ${isActive ? series.color : '#d9e2ec'}`,
                  fontSize: '13px',
                  fontWeight: 700,
                  color: isActive ? '#1e293b' : '#94a3b8',
                  boxShadow: isActive ? `0 0 0 2px ${series.color}18` : 'none',
                  cursor: 'pointer',
                  opacity: isActive ? 1 : 0.78,
                }}
              >
                <span style={{ width: '8px', height: '8px', borderRadius: '999px', background: series.color, opacity: isActive ? 1 : 0.35 }} />
                {series.label}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}

export default function AdDetailModal({ selectedAd, detailLoading, onClose, activeTab, onTabChange, onSave, onOpenDetail, onDownloadMedia }) {
  const [scriptType, setScriptType] = useState('audio'); // 'audio' | 'screen'
  const [brandFilter, setBrandFilter] = useState('all'); // 'all' | 'same'
  const videoRef = useRef(null);

  if (!selectedAd && !detailLoading) return null;

  const handleSeek = (timeStr) => {
    if (!videoRef.current) return;
    // "0:05 - 0:10" or "0:05" -> seconds
    const startPart = timeStr.split('-')[0].trim();
    const parts = startPart.split(':').map(Number);
    let seconds = 0;
    if (parts.length === 2) {
      seconds = parts[0] * 60 + parts[1];
    } else if (parts.length === 3) {
      seconds = parts[0] * 3600 + parts[1] * 60 + parts[2];
    }
    videoRef.current.currentTime = seconds;
    videoRef.current.play();
  };

  const copyToClipboard = (e, text) => {
    e.stopPropagation();
    navigator.clipboard.writeText(text);
    alert('복사되었습니다.');
  };

  const triggerBlobDownload = (blob, filename) => {
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  };

  const handleDownloadScript = () => {
    if (!selectedAd?.extractions) return;
    const e = selectedAd.extractions;
    let text = `[AdInsights] AI 비디오 분석 스크립트 - link: ${selectedAd.ad.meta_library_url || ''}\n\n`;
    
    const sections = [
      { title: '후킹', items: e.hooking_audio || [] },
      { title: '바디', items: e.body_audio || [] },
      { title: '마무리', items: [...(e.closing_audio || []), ...(e.closing_cta ? [{ text: e.closing_cta }] : [])] }
    ];

    sections.forEach((s, idx) => {
      text += `### ${s.title}\n`;
      s.items.forEach(item => {
        if (item.text) text += `${item.text}\n`;
      });
      if (idx < sections.length - 1) text += '\n\n';
    });

    const blob = new Blob([text], { type: 'text/plain' });
    triggerBlobDownload(blob, `script_${selectedAd.ad.library_id}.txt`);
  };

  const handleDownloadMedia = async () => {
    if (!selectedAd?.ad?.library_id) return;

    try {
      const fallbackName = `${selectedAd.ad.brand || 'ad'}_${selectedAd.ad.library_id}.${selectedAd.ad.media_type === 'video' || selectedAd.ad.video_url ? 'mp4' : 'jpg'}`;
      if (onDownloadMedia) {
        await onDownloadMedia(selectedAd.ad.library_id, fallbackName);
      } else {
        throw new Error('download handler missing');
      }
    } catch (err) {
      alert('미디어 다운로드에 실패했습니다.');
    }
  };

  const mediaDownloadLabel = selectedAd?.ad?.media_type === 'video' || selectedAd?.ad?.video_url
    ? '영상 다운로드'
    : '사진 다운로드';

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="modal-card ad-detail-modal" onClick={(e) => e.stopPropagation()}>
        {detailLoading ? (
          <div className="loading">광고 정보를 불러오는 중...</div>
        ) : selectedAd ? (
          <>
            <div className="modal-head">
              <div className="brand-box">
                {selectedAd.ad.brand_logo_url ? (
                  <img
                    className="logo detail-logo"
                    src={selectedAd.ad.brand_logo_url}
                    alt=""
                    referrerPolicy="no-referrer"
                    onError={(e) => { e.target.style.display = 'none'; }}
                  />
                ) : <div className="logo detail-logo" />}
                <div>
                  <h2 style={{ fontSize: '18px', margin: 0 }}>{selectedAd.ad.brand}</h2>
                  <p className="muted" style={{ margin: 0 }}>{selectedAd.ad.workspace_status === 'ended' ? '게재 종료' : '게재 중'}</p>
                </div>
              </div>
              <button className="icon" onClick={onClose} style={{ fontSize: '24px' }}>✕</button>
            </div>

            <div className="detail-layout">
              <div className="detail-media-area">
                {selectedAd.ad.media_type === 'video' || selectedAd.ad.video_url ? (
                  <video
                    ref={videoRef}
                    controls
                    autoPlay
                    muted
                    src={selectedAd.ad.video_url || selectedAd.ad.media_url}
                    poster={selectedAd.ad.video_thumbnail || selectedAd.ad.thumbnail_url || selectedAd.ad.image_url}
                    referrerPolicy="no-referrer"
                  />
                ) : (
                  <img
                    src={selectedAd.ad.image_url || selectedAd.ad.thumbnail_url || selectedAd.ad.media_url}
                    alt=""
                    referrerPolicy="no-referrer"
                    onError={(e) => {
                      const fallback = selectedAd.ad.thumbnail_url || selectedAd.ad.media_url;
                      if (fallback && e.target.src !== fallback) {
                        e.target.src = fallback;
                      }
                    }}
                  />
                )}
              </div>

              <div className="detail-content-area">
                <div className="tabs" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', borderBottom: '1px solid var(--line)', marginBottom: '20px' }}>
                  <div style={{ display: 'flex', gap: '20px' }}>
                    <button
                      className={`tab-btn ${activeTab === 'info' ? 'active' : ''}`}
                      onClick={() => onTabChange('info')}
                      style={{ background: 'none', border: 'none', padding: '10px 0', borderBottom: activeTab === 'info' ? '2px solid var(--navy)' : '2px solid transparent', fontWeight: 700, color: activeTab === 'info' ? 'var(--navy)' : 'var(--muted)', cursor: 'pointer' }}
                    >상세 정보</button>
                    <button
                      className={`tab-btn ${activeTab === 'variations' ? 'active' : ''}`}
                      onClick={() => onTabChange('variations')}
                      style={{ background: 'none', border: 'none', padding: '10px 0', borderBottom: activeTab === 'variations' ? '2px solid var(--navy)' : '2px solid transparent', fontWeight: 700, color: activeTab === 'variations' ? 'var(--navy)' : 'var(--muted)', cursor: 'pointer' }}
                    >연관 소재</button>
                    <button
                      className={`tab-btn ${activeTab === 'metrics' ? 'active' : ''}`}
                      onClick={() => onTabChange('metrics')}
                      style={{ background: 'none', border: 'none', padding: '10px 0', borderBottom: activeTab === 'metrics' ? '2px solid var(--navy)' : '2px solid transparent', fontWeight: 700, color: activeTab === 'metrics' ? 'var(--navy)' : 'var(--muted)', cursor: 'pointer' }}
                    >반응 지표</button>
                  </div>

                  {activeTab === 'variations' && (
                    <div className="brand-filter-tabs" style={{ display: 'flex', gap: '8px', marginBottom: '8px' }}>
                      <button 
                        onClick={() => setBrandFilter('same')}
                        style={{ 
                          padding: '6px 12px', 
                          fontSize: '13px', 
                          borderRadius: '8px', 
                          border: '1px solid var(--line)',
                          background: brandFilter === 'same' ? 'var(--navy)' : '#fff',
                          color: brandFilter === 'same' ? '#fff' : 'var(--ink)',
                          fontWeight: 600,
                          cursor: 'pointer'
                        }}
                      >같은 브랜드</button>
                      <button 
                        onClick={() => setBrandFilter('all')}
                        style={{ 
                          padding: '6px 12px', 
                          fontSize: '13px', 
                          borderRadius: '8px', 
                          border: '1px solid var(--line)',
                          background: brandFilter === 'all' ? 'var(--navy)' : '#fff',
                          color: brandFilter === 'all' ? '#fff' : 'var(--ink)',
                          fontWeight: 600,
                          cursor: 'pointer'
                        }}
                      >전체 브랜드</button>
                    </div>
                  )}
                </div>

                <div className="tab-content">
                  {activeTab === 'info' ? (
                    <div className="info-pane">
                      {selectedAd.extractions ? (
                        <section className="script-section" style={{ marginBottom: '28px' }}>
                          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                            <div style={{ display: 'flex', gap: '12px', alignItems: 'center' }}>
                              <h4 style={{ margin: 0, fontSize: '15px', color: 'var(--navy)', borderLeft: '4px solid var(--navy)', paddingLeft: '8px' }}>
                                AI 비디오 분석
                              </h4>
                              <div className="script-toggle" style={{ display: 'flex', background: '#f1f5f9', borderRadius: '6px', padding: '2px' }}>
                                <button 
                                  onClick={() => setScriptType('audio')}
                                  style={{ border: 0, padding: '4px 10px', fontSize: '12px', borderRadius: '4px', background: scriptType === 'audio' ? '#fff' : 'transparent', fontWeight: 600, color: scriptType === 'audio' ? 'var(--navy)' : 'var(--muted)', cursor: 'pointer', boxShadow: scriptType === 'audio' ? '0 1px 2px rgba(0,0,0,0.1)' : 'none' }}
                                >음성 대본</button>
                                <button 
                                  onClick={() => setScriptType('screen')}
                                  style={{ border: 0, padding: '4px 10px', fontSize: '12px', borderRadius: '4px', background: scriptType === 'screen' ? '#fff' : 'transparent', fontWeight: 600, color: scriptType === 'screen' ? 'var(--navy)' : 'var(--muted)', cursor: 'pointer', boxShadow: scriptType === 'screen' ? '0 1px 2px rgba(0,0,0,0.1)' : 'none' }}
                                >화면 텍스트</button>
                              </div>
                            </div>
                            <button 
                              className="ghost" 
                              onClick={handleDownloadScript}
                              style={{ 
                                padding: '4px 8px', 
                                fontSize: '12px', 
                                display: 'flex', 
                                alignItems: 'center', 
                                gap: '4px',
                                height: '28px',
                                border: '1px solid var(--line)',
                                borderRadius: '6px',
                                background: '#fff',
                                cursor: 'pointer',
                                color: 'var(--navy)',
                                fontWeight: 500
                              }}
                            >
                              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                                <polyline points="7 10 12 15 17 10" />
                                <line x1="12" y1="15" x2="12" y2="3" />
                              </svg>
                              다운받기
                            </button>
                          </div>

                          <div className="script-list" style={{ display: 'grid', gap: '8px' }}>
                            {[
                              ...(selectedAd.extractions.hooking_audio || []).map(a => ({ ...a, section: '후킹' })),
                              ...(selectedAd.extractions.body_audio || []).map(a => ({ ...a, section: '본문' })),
                              ...(selectedAd.extractions.closing_audio || []).map(a => ({ ...a, section: '클로징' })),
                              ...(selectedAd.extractions.hooking_screen_text || []).map(s => ({ ...s, section: '후킹', isScreen: true })),
                              ...(selectedAd.extractions.body_screen_text || []).map(s => ({ ...s, section: '본문', isScreen: true })),
                              ...(selectedAd.extractions.closing_screen_text || []).map(s => ({ ...s, section: '클로징', isScreen: true }))
                            ]
                            .filter(item => scriptType === 'audio' ? !item.isScreen : item.isScreen)
                            .map((item, idx) => {
                              const timeStr = (item.time_range || item.timestamp || '0:00 - 0:00').replace(/~/g, '-').replace(/00:/g, '0:').trim();
                              return (
                                <div 
                                  key={idx} 
                                  className="script-card" 
                                  onClick={() => handleSeek(timeStr)}
                                >
                                  <div className="script-time">
                                    <span className="play-icon">▶</span>
                                    <span>{timeStr}</span>
                                  </div>
                                  <div className="script-text">
                                    {item.text}
                                  </div>
                                  <button className="copy-btn" onClick={(e) => copyToClipboard(e, item.text)} title="복사">
                                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                      <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
                                      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
                                    </svg>
                                  </button>
                                </div>
                              );
                            })}
                          </div>
                        </section>
                      ) : null}

                      <section style={{ marginBottom: '24px' }}>
                        <h4 style={{ margin: '0 0 8px', fontSize: '14px', color: 'var(--muted)' }}>광고 본문 문구</h4>
                        <div style={{ whiteSpace: 'pre-wrap', fontSize: '14px', lineHeight: '1.6', color: 'var(--ink)', padding: '12px', background: '#f7f9fb', borderRadius: '8px' }}>
                          {selectedAd.ad.body || '텍스트 정보가 없습니다.'}
                        </div>
                      </section>
                      <div className="meta-info-grid" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px' }}>
                        <div>
                          <label style={{ fontSize: '12px', color: 'var(--muted)', display: 'block', marginBottom: '4px' }}>시작일</label>
                          <p style={{ margin: 0, fontWeight: 600 }}>{fmtDate(selectedAd.ad.start_date || selectedAd.ad.start_date_text)}</p>
                        </div>
                        <div>
                          <label style={{ fontSize: '12px', color: 'var(--muted)', display: 'block', marginBottom: '4px' }}>총 활성화 시간</label>
                          <p style={{ margin: 0, fontWeight: 600 }}>{daysRunning(selectedAd.ad)}</p>
                        </div>
                        <div>
                          <label style={{ fontSize: '12px', color: 'var(--muted)', display: 'block', marginBottom: '4px' }}>
                            {mediaDownloadLabel}
                          </label>
                          <button 
                            className="ghost" 
                            onClick={handleDownloadMedia}
                            style={{ 
                              width: '100%',
                              padding: '4px 8px', 
                              fontSize: '13px', 
                              display: 'flex', 
                              alignItems: 'center', 
                              justifyContent: 'center',
                              gap: '6px',
                              height: '32px',
                              border: '1px solid var(--line)',
                              borderRadius: '6px',
                              background: '#fff',
                              cursor: 'pointer',
                              color: 'var(--navy)',
                              fontWeight: 600
                            }}
                          >
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                              <polyline points="7 10 12 15 17 10" />
                              <line x1="12" y1="15" x2="12" y2="3" />
                            </svg>
                            {mediaDownloadLabel}
                          </button>
                        </div>
                        <div style={{ gridColumn: 'span 2' }}>
                          <label style={{ fontSize: '12px', color: 'var(--muted)', display: 'block', marginBottom: '4px' }}>랜딩 페이지</label>
                          <a href={selectedAd.ad.link_url} target="_blank" rel="noreferrer" style={{ fontSize: '14px', color: 'var(--blue)', wordBreak: 'break-all' }}>{selectedAd.ad.link_url || '-'}</a>
                        </div>
                        {selectedAd.ad.instagram_permalink ? (
                          <div style={{ gridColumn: 'span 2' }}>
                            <label style={{ fontSize: '12px', color: 'var(--muted)', display: 'block', marginBottom: '4px' }}>Instagram Permalink</label>
                            <a
                              href={selectedAd.ad.instagram_permalink}
                              target="_blank"
                              rel="noreferrer"
                              style={{ fontSize: '14px', color: 'var(--blue)', wordBreak: 'break-all' }}
                            >
                              {selectedAd.ad.instagram_permalink}
                            </a>
                          </div>
                        ) : null}
                      </div>
                    </div>
                  ) : activeTab === 'variations' ? (
                    <div className="variations-pane">
                      <div className="variation-list" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '20px' }}>
                        {(() => {
                          const filtered = (selectedAd.variations || []).filter(v => 
                            brandFilter === 'all' || v.page_id === selectedAd.ad.page_id
                          );
                          return filtered.length > 0 ? filtered.map((v) => (
                            <div key={v.library_id} className="variation-item" onClick={() => onOpenDetail(v.library_id)} style={{ cursor: 'pointer', display: 'flex', flexDirection: 'column', gap: '10px' }}>
                              <div style={{ aspectRatio: '4/3', background: '#f0f2f5', borderRadius: '12px', overflow: 'hidden', border: '1px solid var(--line)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                                <img src={v.thumbnail_url || v.image_url} alt="" style={{ width: '100%', height: '100%', display: 'block', objectFit: 'contain' }} />
                              </div>
                              <div className="variation-meta">
                                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '4px' }}>
                                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flex: 1, minWidth: 0 }}>
                                    {v.brand_logo_url ? (
                                      <img src={v.brand_logo_url} alt="" style={{ width: '16px', height: '16px', borderRadius: '50%' }} />
                                    ) : (
                                      <div style={{ width: '16px', height: '16px', borderRadius: '50%', background: '#eee' }} />
                                    )}
                                    <span style={{ fontSize: '13px', fontWeight: 700, color: 'var(--navy)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                                      {v.brand || '-'}
                                    </span>
                                  </div>
                                  <span className={`status ${v.status === 'ended' ? 'ended' : ''}`} style={{ fontSize: '10px', padding: '2px 6px', flexShrink: 0 }}>
                                    {v.status === 'ended' ? '게재 종료' : '게재 중'}
                                  </span>
                                </div>
                                <div style={{ fontSize: '11px', color: 'var(--muted)', marginBottom: '6px' }}>
                                  시작일: {fmtDate(v.start_date || v.start_date_text)}
                                </div>
                                
                                {(Number(v.instagram_view_count) > 0 || Number(v.instagram_like_count) > 0 || Number(v.instagram_comments_count) > 0) && (
                                  <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                                    {Number(v.instagram_view_count) > 0 && (
                                      <span style={{ display: 'flex', alignItems: 'center', gap: '3px', fontSize: '11px', color: '#4f6ee8', fontWeight: 600 }}>
                                        <svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor">
                                          <path d="M5.888 22.5a3.46 3.46 0 0 1-1.721-.46 3.394 3.46 0 0 1-1.667-3.04v-14a3.394 3.46 0 0 1 1.667-3.04 3.461 3.461 0 0 1 3.334 0l12 7a3.456 3.46 0 0 1 0 6.08l-12 7a3.457 3.46 0 0 1-1.613.46Z"/>
                                        </svg>
                                        {fmtMetric(v.instagram_view_count)}
                                      </span>
                                    )}
                                    {Number(v.instagram_like_count) > 0 && (
                                      <span style={{ display: 'flex', alignItems: 'center', gap: '3px', fontSize: '11px', color: '#c54763', fontWeight: 600 }}>
                                        <svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor">
                                          <path d="M16.792 3.904A4.989 4.989 0 0 1 21.5 9.122c0 3.072-2.652 4.959-5.197 7.222-2.512 2.243-3.865 3.469-4.303 3.752-.477-.309-2.143-1.823-4.303-3.752C5.141 14.077 2.5 12.191 2.5 9.122a4.989 4.989 0 0 1 4.708-5.218 4.21 4.21 0 0 1 3.675 1.941c.03.044.07.086.117.126a4.21 4.21 0 0 1 3.675-1.941 1.02 1.02 0 0 1 2.117-.126Z"/>
                                        </svg>
                                        {fmtMetric(v.instagram_like_count)}
                                      </span>
                                    )}
                                  </div>
                                )}
                              </div>
                            </div>
                          )) : <p className="muted">유사한 광고가 없습니다.</p>;
                        })()}
                      </div>
                    </div>
                  ) : (
                    <div className="metrics-pane">
                      <MetricTrendChartInteractive history={selectedAd.metric_history || []} />
                    </div>
                  )}
                </div>
              </div>
            </div>

            <div className="modal-footer" style={{ marginTop: '24px', display: 'flex', gap: '12px' }}>
              <a className="ghost" href={selectedAd.ad.meta_library_url} target="_blank" rel="noreferrer" style={{ flex: 1, textAlign: 'center', textDecoration: 'none' }}>Meta에서 보기</a>
              <button className="primary" onClick={() => onSave(selectedAd.ad.library_id)} style={{ flex: 2 }}>
                {selectedAd.ad.saved ? '저장됨' : '광고 저장하기'}
              </button>
            </div>
          </>
        ) : null}
      </div>
    </div>
  );
}

