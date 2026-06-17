'use client';

import Link from 'next/link';
import { useState, useEffect, useRef } from 'react';
import { useSearchParams } from 'next/navigation';
import { fmtAgo } from '../lib/utils';

export default function Sidebar({ 
  folders, 
  competitors, 
  activePage, 
  folderItemCountById,
  folderItemUnit,
  onAddOpen, 
  onCreateFolder,
  onDeleteFolder,
  onRenameFolder,
  reorderFolders,
  reorderCompetitors,
  onUpdateMonitor,
  onMoveCompetitor,
  onDeleteCompetitor,
  onDeleteCompetitors,
  selectedFilterBrands,
  onToggleFilterBrand,
  activeLibraryFolderId,
  onSelectLibraryFolder,
  searchQuery,
  setSearchQuery,
  setError
}) {
  const [menuOpenId, setMenuOpenId] = useState(null);
  const [folderMenuOpenId, setFolderMenuOpenId] = useState(null);
  const [moveOpenId, setMoveOpenId] = useState(null);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteSelectedIds, setDeleteSelectedIds] = useState(new Set());
  const [isDeleting, setIsDeleting] = useState(false);
  const [draggedFolderId, setDraggedFolderId] = useState(null);
  const [dragOverId, setDragOverId] = useState(null);
  const [dragDirection, setDragDirection] = useState(null); // 'top' or 'bottom'
  const [draggedCompetitorId, setDraggedCompetitorId] = useState(null);
  const [dragOverCompetitorId, setDragOverCompetitorId] = useState(null);
  const [competitorDragDirection, setCompetitorDragDirection] = useState(null);

  const searchParams = useSearchParams();
  const currentView = searchParams.get('view') || 'top10';

  const sidebarRef = useRef(null);

  // 드래그 중 수동 스크롤을 위한 로직
  useEffect(() => {
    if (draggedFolderId === null && draggedCompetitorId === null) return;

    const handleWheel = (e) => {
      if (sidebarRef.current) {
        // 드래그 중 브라우저가 기본 스크롤을 막는 것을 방지하고 강제 스크롤
        e.preventDefault();
        sidebarRef.current.scrollTop += e.deltaY;
      }
    };

    // window 레벨에서 capture: true로 이벤트를 먼저 가로챔
    window.addEventListener('wheel', handleWheel, { passive: false, capture: true });
    return () => window.removeEventListener('wheel', handleWheel, { capture: true });
  }, [draggedFolderId, draggedCompetitorId]);

  // Close menu when clicking outside
  useEffect(() => {
    const close = () => {
      setMenuOpenId(null);
      setFolderMenuOpenId(null);
    };
    window.addEventListener('click', close);
    return () => window.removeEventListener('click', close);
  }, []);

  const sortCompetitorItems = (items) => [...items];

  const getSortedCompetitorsInFolder = (folderId) => sortCompetitorItems(
    competitors.filter((item) => (folderId == null ? item.folder_id == null : item.folder_id === folderId))
  );

  const competitorsByFolder = folders.map(folder => ({
    ...folder,
    competitors: sortCompetitorItems(competitors.filter(c => c.folder_id === folder.id))
  }));
  const unorganizedCompetitors = sortCompetitorItems(competitors.filter(c => !c.folder_id));
  const isLibrary = activePage === 'library';
  const deleteGroups = [
    ...competitorsByFolder.filter(group => group.competitors.length > 0)
  ].filter(group => group.competitors.length > 0);

  const openDeleteModal = (competitorId) => {
    setDeleteSelectedIds(new Set([competitorId]));
    setDeleteOpen(true);
  };

  const toggleDeleteSelection = (competitorId) => {
    setDeleteSelectedIds(current => {
      const next = new Set(current);
      if (next.has(competitorId)) next.delete(competitorId);
      else next.add(competitorId);
      return next;
    });
  };

  const closeDeleteModal = () => {
    if (isDeleting) return;
    setDeleteOpen(false);
    setDeleteSelectedIds(new Set());
  };

  const confirmDeleteBrands = async () => {
    const ids = Array.from(deleteSelectedIds);
    if (ids.length === 0) return;
    setIsDeleting(true);
    try {
      const handler = onDeleteCompetitors || ((selectedIds) => Promise.all(selectedIds.map(id => onDeleteCompetitor(id))));
      await handler(ids);
      setDeleteOpen(false);
      setDeleteSelectedIds(new Set());
    } catch (err) {
      setError(err.message);
    } finally {
      setIsDeleting(false);
    }
  };

  const handleDragStart = (e, competitorId) => {
    setDraggedCompetitorId(competitorId);
    e.dataTransfer.setData('competitorId', String(competitorId));
    e.dataTransfer.effectAllowed = 'move';
  };

  const handleDropCompetitor = (e, targetFolderId) => {
    e.preventDefault();
    const competitorId = Number(e.dataTransfer.getData('competitorId'));
    if (competitorId) {
      onMoveCompetitor(competitorId, targetFolderId).catch(err => setError(err.message));
    }
  };

  const handleFolderDragStart = (e, folderId) => {
    setDraggedFolderId(folderId);
    e.dataTransfer.effectAllowed = 'move';
  };

  const handleFolderDragOver = (e, targetFolderId) => {
    e.preventDefault();
    if (draggedFolderId === null) return;
    
    // 폴더 순서 변경 애니메이션을 위한 위치 계산
    if (targetFolderId && draggedFolderId !== targetFolderId) {
      const rect = e.currentTarget.getBoundingClientRect();
      const midY = rect.top + rect.height / 2;
      const direction = e.clientY < midY ? 'top' : 'bottom';
      
      setDragOverId(targetFolderId);
      setDragDirection(direction);
    }
    
    e.dataTransfer.dropEffect = 'move';
  };

  const handleFolderDrop = (e, targetFolderId) => {
    e.preventDefault();
    const direction = dragDirection;
    setDragOverId(null);
    setDragDirection(null);
    
    if (draggedFolderId === null || draggedFolderId === targetFolderId) return;

    // 드래그 중인 요소를 제외한 나머지 ID 리스트
    const filteredIds = folders.map(f => f.id).filter(id => id !== draggedFolderId);
    const targetIndex = filteredIds.indexOf(targetFolderId);
    
    if (targetIndex === -1) return;

    // 방향에 따라 대상의 앞(top) 또는 뒤(bottom)에 삽입
    const insertIndex = direction === 'top' ? targetIndex : targetIndex + 1;
    const newIds = [...filteredIds];
    newIds.splice(insertIndex, 0, draggedFolderId);

    reorderFolders(newIds).catch(err => setError(err.message));
    setDraggedFolderId(null);
  };

  const handleCompetitorDragOver = (e, targetCompetitorId) => {
    e.preventDefault();
    const competitorId = Number(e.dataTransfer.getData('competitorId'));
    if (!competitorId || competitorId === targetCompetitorId) return;

    const draggedCompetitor = competitors.find((item) => item.id === competitorId);
    const targetCompetitor = competitors.find((item) => item.id === targetCompetitorId);
    if (!draggedCompetitor || !targetCompetitor) return;
    if (draggedCompetitor.folder_id !== targetCompetitor.folder_id) return;

    const rect = e.currentTarget.getBoundingClientRect();
    const midY = rect.top + rect.height / 2;
    const direction = e.clientY < midY ? 'top' : 'bottom';
    setDragOverCompetitorId(targetCompetitorId);
    setCompetitorDragDirection(direction);
    e.dataTransfer.dropEffect = 'move';
  };

  const handleCompetitorDrop = (e, targetCompetitorId) => {
    e.preventDefault();
    const competitorId = Number(e.dataTransfer.getData('competitorId'));
    const direction = competitorDragDirection;
    setDragOverCompetitorId(null);
    setCompetitorDragDirection(null);

    if (!competitorId || competitorId === targetCompetitorId) return;

    const draggedCompetitor = competitors.find((item) => item.id === competitorId);
    const targetCompetitor = competitors.find((item) => item.id === targetCompetitorId);
    if (!draggedCompetitor || !targetCompetitor) return;
    if (draggedCompetitor.folder_id !== targetCompetitor.folder_id) return;

    const folderCompetitors = getSortedCompetitorsInFolder(targetCompetitor.folder_id ?? null);
    const filteredIds = folderCompetitors.map((item) => item.id).filter((id) => id !== competitorId);
    const targetIndex = filteredIds.indexOf(targetCompetitorId);
    if (targetIndex === -1) return;

    const insertIndex = direction === 'top' ? targetIndex : targetIndex + 1;
    const newIds = [...filteredIds];
    newIds.splice(insertIndex, 0, competitorId);
    if (!reorderCompetitors) return;
    reorderCompetitors(targetCompetitor.folder_id ?? null, newIds).catch(err => setError(err.message));
  };

  const renderCompetitorRow = (c) => (
    <article 
      className={`competitor-row ${draggedCompetitorId === c.id ? 'dragging' : ''} ${dragOverCompetitorId === c.id ? `drag-over-${competitorDragDirection}` : ''}`}
      key={c.id} 
      draggable 
      onDragStart={(e) => handleDragStart(e, c.id)}
      onDragOver={(e) => handleCompetitorDragOver(e, c.id)}
      onDrop={(e) => handleCompetitorDrop(e, c.id)}
      onDragEnd={() => {
        setDraggedCompetitorId(null);
        setDragOverCompetitorId(null);
        setCompetitorDragDirection(null);
      }}
      onClick={() => onToggleFilterBrand(c.id)}
    >
      <div className="row-top">
        <div className="row-main">
          <input 
            type="checkbox" 
            checked={selectedFilterBrands.has(c.id)} 
            onChange={() => onToggleFilterBrand(c.id)}
            onClick={(e) => e.stopPropagation()}
          />
          {c.brand_logo_url ? <img className="small-logo" src={c.brand_logo_url} alt="" /> : <div className="small-logo" />}
          <div className="brand-detail">
            <div className="name" title={c.brand}>{c.brand}</div>
          </div>
        </div>
        <div className="row-right">
          <div className="row-meta">{fmtAgo(c.last_seen_at)}</div>
          <div className="menu-container">
            <button 
              className="menu-dot-btn" 
              onClick={(e) => {
                e.stopPropagation();
                setMenuOpenId(menuOpenId === c.id ? null : c.id);
              }}
            >•••</button>
            {menuOpenId === c.id && (
              <div className="brand-menu" onClick={(e) => e.stopPropagation()}>
                <button onClick={() => {
                  setMenuOpenId(null);
                  setMoveOpenId(c.id);
                }}>📁 폴더로 이동</button>
                <button className="danger" onClick={() => {
                  setMenuOpenId(null);
                  openDeleteModal(c.id);
                }}>🗑️ 브랜드 삭제</button>
              </div>
            )}
          </div>
        </div>
      </div>
      <div className="row-bottom">
        <div className="stats-spacer" />
        <div className="stats">사진 {c.image_ads} / 영상 {c.video_ads}</div>
      </div>
    </article>
  );

  return (
    <aside className="sidebar" ref={sidebarRef}>
      <div className="brand-header">
        <div className="logo-box">
          <div className="brand-icon">Ad</div>
        </div>
        <div className="brand-info">
          <div className="brand-name">AdInsights</div>
          <div className="brand-sub">B2B Enterprise</div>
        </div>
      </div>

      <div className="sidebar-search">
        <input 
          placeholder="브랜드명"
 
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && onAddOpen()}
        />
        <button className="primary add-btn" onClick={onAddOpen}>+ 추가</button>
      </div>

      <nav className="main-nav">
        <Link href="/" className={activePage === 'monitoring' ? 'active' : ''}>
          <span className="icon">📊</span>
          모니터링
        </Link>
        <Link href="/library" className={activePage === 'library' ? 'active' : ''}>
          <span className="icon">📂</span>
          라이브러리
        </Link>
        <Link href="/creative-composition" className={activePage === 'creative-composition' ? 'active' : ''}>
          <span className="icon">▦</span>
          광고 소재 구성(Beta)
        </Link>
        {activePage === 'creative-composition' && (
          <div className="sub-nav">
            <Link href="/creative-composition?view=top10" className={`sub-link ${currentView === 'top10' ? 'active' : ''}`}>TOP 10 / 상위 구성</Link>
            <Link href="/creative-composition?view=by-type" className={`sub-link ${currentView === 'by-type' ? 'active' : ''}`}>구성 종류별 보기</Link>
            <Link href="/creative-composition?view=by-brand" className={`sub-link ${currentView === 'by-brand' ? 'active' : ''}`}>브랜드별 보기</Link>
          </div>
        )}
      </nav>

      <section className="folders-section">
        <div className="section-header">
          <span>내 폴더</span>
          <button className="icon-btn" onClick={() => {
            const name = prompt('새 폴더 이름을 입력하세요:');
            if (name) onCreateFolder(name).catch(err => setError(err.message));
          }}>+</button>
        </div>
        
        <div className="folder-list">
          {competitorsByFolder.map(folder => (
            <details 
              key={folder.id} 
              className={`folder-group ${draggedFolderId === folder.id ? 'dragging' : ''} ${dragOverId === folder.id ? `drag-over-${dragDirection}` : ''}`} 
              open={!isLibrary}
              onDragOver={(e) => handleFolderDragOver(e, folder.id)}
              onDrop={(e) => {
                const competitorId = e.dataTransfer.getData('competitorId');
                if (competitorId) {
                  handleDropCompetitor(e, folder.id);
                } else {
                  handleFolderDrop(e, folder.id);
                }
              }}
            >
              <summary 
                className={`folder-item ${isLibrary && activeLibraryFolderId === folder.id ? 'active' : ''}`}
                draggable
                onDragStart={(e) => handleFolderDragStart(e, folder.id)}
                onDragEnd={() => {
                  setDraggedFolderId(null);
                  setDragOverId(null);
                  setDragDirection(null);
                }}
                onClick={(e) => {
                  if (isLibrary) {
                    e.preventDefault();
                    onSelectLibraryFolder?.(folder.id);
                    return;
                  }
                  // 아이콘이나 체브론 영역이 아닌 곳을 클릭하면 토글 방지
                  if (!e.target.closest('.folder-icon') && !e.target.closest('.chevron')) {
                    e.preventDefault();
                  }
                }}
              >
                <div className="folder-name-wrap">
                  <span className="folder-icon">📁</span>
                  <span className="folder-name">{folder.name}</span>
                </div>
                <div className="folder-meta-container">
                  <div className="folder-meta-default">
                    {isLibrary ? (folderItemCountById?.[folder.id] ?? 0) : folder.competitors.length}{' '}
                    {isLibrary ? (folderItemUnit || '항목') : '브랜드'} 
                    <span className="chevron">▾</span>
                  </div>
                  <div className="folder-hover-actions">
                    <div className="menu-container">
                      <button 
                        className="menu-dot-btn" 
                        onClick={(e) => {
                          e.preventDefault();
                          e.stopPropagation();
                          setFolderMenuOpenId(folderMenuOpenId === folder.id ? null : folder.id);
                        }}
                      >•••</button>
                      {folderMenuOpenId === folder.id && (
                        <div className="brand-menu" onClick={(e) => e.stopPropagation()}>
                          <button onClick={() => {
                            setFolderMenuOpenId(null);
                            onRenameFolder(folder.id, folder.name);
                          }}>✏️ 이름 변경</button>
                          <button className="danger" onClick={() => {
                            setFolderMenuOpenId(null);
                            onDeleteFolder(folder.id);
                          }}>🗑️ 폴더 삭제</button>
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              </summary>
              {!isLibrary ? (
                <div className="folder-content">
                  {folder.competitors.map(renderCompetitorRow)}
                </div>
              ) : null}
            </details>
          ))}
        </div>
      </section>

      <div className="sidebar-footer">
        <a className="help-link">❓ 도움말</a>
      </div>

      {moveOpenId && (
        <div className="dialog-backdrop" onClick={() => setMoveOpenId(null)}>
          <div className="modal-card mini" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <h2>폴더 이동</h2>
              <button className="icon" onClick={() => setMoveOpenId(null)}>닫기</button>
            </div>
            <div className="folder-select-list">
              {folders.map(f => (
                <button 
                  key={f.id} 
                  className="folder-select-item" 
                  onClick={() => {
                    onMoveCompetitor(moveOpenId, f.id).catch(err => setError(err.message));
                    setMoveOpenId(null);
                  }}
                >
                  📁 {f.name}
                </button>
              ))}
            </div>
          </div>
        </div>
      )}

      {deleteOpen && (
        <div className="dialog-backdrop" onClick={closeDeleteModal}>
          <div className="modal-card delete-brands-modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <div>
                <h2>브랜드 삭제</h2>
                <p className="modal-subtitle">삭제할 브랜드를 선택하세요.</p>
              </div>
              <button className="icon" onClick={closeDeleteModal} disabled={isDeleting}>닫기</button>
            </div>

            <div className="delete-brand-groups">
              {deleteGroups.map(group => (
                <section className="delete-brand-group" key={group.id}>
                  <div className="delete-brand-group-title">
                    <span>{group.name}</span>
                    <span>{group.competitors.length} 브랜드</span>
                  </div>
                  <div className="delete-brand-grid">
                    {group.competitors.map(c => (
                      <label className="delete-brand-item" key={c.id}>
                        <input
                          type="checkbox"
                          checked={deleteSelectedIds.has(c.id)}
                          onChange={() => toggleDeleteSelection(c.id)}
                          disabled={isDeleting}
                        />
                        {c.brand_logo_url ? (
                          <img className="small-logo" src={c.brand_logo_url} alt="" />
                        ) : (
                          <div className="small-logo" />
                        )}
                        <span title={c.brand}>{c.brand}</span>
                      </label>
                    ))}
                  </div>
                </section>
              ))}
            </div>

            <div className="modal-actions">
              <button className="ghost" type="button" onClick={closeDeleteModal} disabled={isDeleting}>
                취소
              </button>
              <button
                className="primary danger-action"
                type="button"
                onClick={confirmDeleteBrands}
                disabled={isDeleting || deleteSelectedIds.size === 0}
              >
                {isDeleting ? '삭제 중...' : `삭제하기 (${deleteSelectedIds.size})`}
              </button>
            </div>
          </div>
        </div>
      )}
    </aside>
  );
}
