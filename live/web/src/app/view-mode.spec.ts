import { COMPACT_KEY, ViewMode, initialCompact, isCompactKey } from './view-mode';

const key = (over: Partial<KeyboardEvent> & { target?: unknown } = {}) =>
  ({ key: 'c', ctrlKey: false, metaKey: false, altKey: false, repeat: false, target: document.body, ...over }) as KeyboardEvent;

describe('compact mode: initial value', () => {
  it('uses the saved choice, full view by default', () => {
    expect(initialCompact('', null)).toBe(false);
    expect(initialCompact('', '1')).toBe(true);
    expect(initialCompact('', '0')).toBe(false);
  });

  it('lets ?compact=1 / ?compact=0 win over the saved choice', () => {
    expect(initialCompact('?compact=1', '0')).toBe(true);
    expect(initialCompact('?x=2&compact=true', null)).toBe(true);
    expect(initialCompact('?compact=0', '1')).toBe(false);
    expect(initialCompact('?compact=maybe', '1')).toBe(true); // unknown value: ignored
  });
});

describe('compact mode: the c key', () => {
  it('toggles on a bare c (either case)', () => {
    expect(isCompactKey(key())).toBe(true);
    expect(isCompactKey(key({ key: 'C' }))).toBe(true);
    expect(isCompactKey(key({ key: 'x' }))).toBe(false);
  });

  it('ignores shortcuts and key repeat', () => {
    expect(isCompactKey(key({ ctrlKey: true }))).toBe(false); // Ctrl+C copies
    expect(isCompactKey(key({ metaKey: true }))).toBe(false);
    expect(isCompactKey(key({ altKey: true }))).toBe(false);
    expect(isCompactKey(key({ repeat: true }))).toBe(false);
  });

  it('ignores typing in a field', () => {
    for (const tag of ['input', 'textarea', 'select']) {
      expect(isCompactKey(key({ target: document.createElement(tag) }))).toBe(false);
    }
    const div = document.createElement('div');
    Object.defineProperty(div, 'isContentEditable', { value: true });
    expect(isCompactKey(key({ target: div }))).toBe(false);
    expect(isCompactKey(key({ target: document.createElement('button') }))).toBe(true);
  });
});

describe('compact mode: toggle and persistence', () => {
  beforeEach(() => localStorage.clear());

  it('starts from the saved choice and saves each toggle', () => {
    const m = new ViewMode();
    expect(m.compact()).toBe(false);
    m.toggle();
    expect(m.compact()).toBe(true);
    expect(localStorage.getItem(COMPACT_KEY)).toBe('1');
    expect(new ViewMode().compact()).toBe(true); // a reload keeps it
    m.toggle();
    expect(localStorage.getItem(COMPACT_KEY)).toBe('0');
    expect(new ViewMode().compact()).toBe(false);
  });

  it('still toggles when storage is blocked', () => {
    const get = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked'); });
    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked'); });
    try {
      const m = new ViewMode();
      expect(m.compact()).toBe(false);
      m.toggle();
      expect(m.compact()).toBe(true);
    } finally { get.mockRestore(); set.mockRestore(); }
  });
});
