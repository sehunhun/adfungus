import Link from 'next/link';
import { useState, useEffect } from 'react';
import { fmtAgo } from '../lib/utils';

export default function Sidebar({ 
  folders, 
  competitors, 
  activePage, 
  onAddOpen, 
  onCreateFolder,
  onDeleteFolder,
  onRenameFolder,
  reorderFolders,
  onUpdateMonitor,
  onMoveCompetitor,
  onDeleteCompetitor,
  onDeleteCompetitors,
  selectedFilterBrands,
  onToggleFilterBrand,
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

  // Close menu when clicking outside
  useEffect(() => {
    const close = () => {
      setMenuOpenId(null);
      setFolderMenuOpenId(null);
    };
    window.addEventListener('click', close);
    return () => window.removeEventListener('click', close);
  }, []);

  const competitorsByFolder = folders.map(folder => ({
    ...folder,
    competitors: competitors.filter(c => c.folder_id === folder.id)
  }));
  const unorganizedCompetitors = competitors.filter(c => !c.folder_id);
  const deleteGroups = [
    ...competitorsByFolder.filter(group => group.competitors.length > 0),
    { id: 'unorganized', name: '미지정', competitors: unorganizedCompetitors }
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
    e.dataTransfer.setData('competitorId', competitorId);
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

  const handleFolderDragOver = (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
  };

  const handleFolderDrop = (e, targetFolderId) => {
    e.preventDefault();
    if (draggedFolderId === null || draggedFolderId === targetFolderId) return;

    const folderIds = folders.map(f => f.id);
    const draggedIndex = folderIds.indexOf(draggedFolderId);
    const targetIndex = folderIds.indexOf(targetFolderId);

    if (draggedIndex === -1 || targetIndex === -1) return;

    const newIds = [...folderIds];
    const [movedId] = newIds.splice(draggedIndex, 1);
    newIds.splice(targetIndex, 0, movedId);

    reorderFolders(newIds).catch(err => setError(err.message));
    setDraggedFolderId(null);
  };

  const renderCompetitorRow = (c) => (
    <article 
      className="competitor-row" 
      key={c.id} 
      draggable 
      onDragStart={(e) => handleDragStart(e, c.id)}
      onClick={() => onToggleFilterBrand(c.id)}
    >
      <div className="row-top">
        <div className="row-main">
          <input 
            type="checkbox" 
            checked={selectedFilterBrands.has(c.id)} 
            readOnly 
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
    <aside className="sidebar">
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
            <details key={folder.id} className="folder-group" open>
              <summary 
                className="folder-item"
                draggable
                onDragStart={(e) => handleFolderDragStart(e, folder.id)}
                onDragEnd={() => setDraggedFolderId(null)}
                onDragOver={handleFolderDragOver}
                onDrop={(e) => {
                  const competitorId = e.dataTransfer.getData('competitorId');
                  if (competitorId) {
                    handleDropCompetitor(e, folder.id);
                  } else {
                    handleFolderDrop(e, folder.id);
                  }
                }}
                onClick={(e) => {
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
                    {folder.competitors.length} 브랜드 
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
              <div className="folder-content">
                {folder.competitors.map(renderCompetitorRow)}
              </div>
            </details>
          ))}

          <details 
            className="folder-group" 
            open
            onDragOver={handleFolderDragOver}
            onDrop={(e) => handleDropCompetitor(e, null)}
          >
            <summary 
              className="folder-item"
              onClick={(e) => {
                if (!e.target.closest('.folder-icon') && !e.target.closest('.chevron')) {
                  e.preventDefault();
                }
              }}
            >
              <div className="folder-name-wrap">
                <span className="folder-icon">📁</span>
                <span className="folder-name">미지정</span>
              </div>
              <div className="folder-meta-container">
                <div className="folder-meta-default">
                  {unorganizedCompetitors.length} 브랜드 
                  <span className="chevron">▾</span>
                </div>
              </div>
            </summary>
            <div className="folder-content">
              {unorganizedCompetitors.map(renderCompetitorRow)}
            </div>
          </details>
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
              <button 
                className="folder-select-item" 
                onClick={() => {
                  onMoveCompetitor(moveOpenId, null).catch(err => setError(err.message));
                  setMoveOpenId(null);
                }}
              >
                📁 미지정
              </button>
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
