import { useState, useRef, useEffect } from 'react';
import { fmtDate, daysRunning } from '../lib/utils';

export default function AdDetailModal({ selectedAd, detailLoading, onClose, activeTab, onTabChange, onSave, onOpenDetail }) {
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
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `script_${selectedAd.ad.library_id}.txt`;
    a.click();
    URL.revokeObjectURL(url);
  };

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
                                >음성 자막</button>
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
                          <label style={{ fontSize: '12px', color: 'var(--muted)', display: 'block', marginBottom: '4px' }}>소재 다운로드</label>
                          <button 
                            className="ghost" 
                            onClick={() => {
                              const url = selectedAd.ad.video_url || selectedAd.ad.media_url || selectedAd.ad.image_url;
                              if (url) window.open(url, '_blank');
                            }}
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
                            다운받기
                          </button>
                        </div>
                        <div style={{ gridColumn: 'span 2' }}>
                          <label style={{ fontSize: '12px', color: 'var(--muted)', display: 'block', marginBottom: '4px' }}>랜딩 페이지</label>
                          <a href={selectedAd.ad.link_url} target="_blank" rel="noreferrer" style={{ fontSize: '14px', color: 'var(--blue)', wordBreak: 'break-all' }}>{selectedAd.ad.link_url || '-'}</a>
                        </div>
                      </div>
                    </div>
                  ) : (
                    <div className="variations-pane">
                      <div className="variation-list" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 1fr', gap: '16px' }}>
                        {(() => {
                          const filtered = (selectedAd.variations || []).filter(v => 
                            brandFilter === 'all' || v.page_id === selectedAd.ad.page_id
                          );
                          return filtered.length > 0 ? filtered.map((v) => (
                            <div key={v.library_id} className="variation-item" onClick={() => onOpenDetail(v.library_id)} style={{ cursor: 'pointer' }}>
                              <div style={{ aspectRatio: '1', background: '#f0f2f5', borderRadius: '8px', overflow: 'hidden', marginBottom: '6px' }}>
                                <img src={v.thumbnail_url || v.image_url} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
                              </div>
                              <div style={{ fontSize: '11px', color: 'var(--muted)', textAlign: 'center' }}>
                                ID: {v.library_id}
                              </div>
                            </div>
                          )) : <p className="muted">유사한 광고가 없습니다.</p>;
                        })()}
                      </div>
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

